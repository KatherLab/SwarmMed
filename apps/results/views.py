from django.conf import settings
import os
import logging
import ast # Import the ast module

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from apps.project.models import Project, UserCurrentProject
from apps.training.models import TrainingJob
from .models import TrainingResult
from .tasks import sync_project_results
from apps.data.utils import get_s3_download_url, get_s3_client
from django.http import HttpResponse
import zipfile
import io

logger = logging.getLogger(__name__)

def get_user_project(request):
    """
    Get the current user's active project identifier.
    
    Args:
        request: Django request object
        
    Returns:
        tuple: (project_uuid, is_valid)
            - project_uuid: String UUID of the project or None
            - is_valid: Boolean indicating if a valid project was found
    """
    try:
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        if not user_current_project.project:
            return None, False
        
        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False


@login_required(login_url='/users/signin/')
def results(request):
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return render(request, "apps/results/no_project_selected.html", {"segment": "results"})

    project = Project.objects.get(identifier=current_project_uuid)

    # Trigger background sync
    sync_project_results.delay(current_project_uuid)
    messages.info(request, "Result synchronization has been started in the background. The page will refresh automatically.")

    # Sync S3 objects under <project>/results/<flare_job_id>/ to DB if missing
    s3 = get_s3_client()
    prefix = f"{project.identifier}/results/"
    paginator = s3.get_paginator('list_objects_v2')
    job_ids_in_s3 = set()
    for page in paginator.paginate(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            if key.endswith('/'):
                continue
            # Expect keys like <project_uuid>/results/<job_id>/<filename>
            parts = key.split('/')
            if len(parts) < 4:
                continue
            job_id = parts[2]
            job_ids_in_s3.add(job_id)
            # Resolve job by comparing extracted actual flare_job_id
            job = None
            for j in TrainingJob.objects.filter(project=project):
                raw = j.flare_job_id or ""
                actual = None
                try:
                    parsed = ast.literal_eval(raw)
                    if isinstance(parsed, list):
                        for it in parsed:
                            if isinstance(it, dict) and it.get('type') == 'string':
                                data = it.get('data', '')
                                if 'Submitted job:' in data:
                                    actual = data.split(':')[-1].strip()
                                    break
                except (ValueError, SyntaxError):
                    pass
                if not actual:
                    actual = raw
                if actual and actual.strip() == job_id:
                    job = j
                    break
            if not job:
                continue
            
            # Upsert TrainingResult
            tr, created = TrainingResult.objects.get_or_create(
                job=job,
                file_path=key,
                defaults={'file_size': obj.get('Size', 0)}
            )
            if not created and tr.file_size != obj.get('Size', 0):
                tr.file_size = obj.get('Size', 0)
                tr.save(update_fields=['file_size'])

    training_results_queryset = TrainingResult.objects.filter(job__project=project).order_by('file_path')

    # Prepare results for template, converting UUIDs to strings
    prepared_results = []
    job_options_seen = set(job_ids_in_s3)  # start with S3 job IDs to ensure all appear
    job_options = []
    for job_id in sorted(job_ids_in_s3):
        job_options.append({'value': job_id, 'label': job_id[:36]})
    for result in training_results_queryset:
        flare_job_id_raw = result.job.flare_job_id
        
        # Attempt to parse flare_job_id_raw if it's a string representation of a list
        actual_flare_job_id = ""
        logger.info(f"flare_job_id_raw: {flare_job_id_raw}")
        try:
            parsed_list = ast.literal_eval(flare_job_id_raw)
            if isinstance(parsed_list, list) and len(parsed_list) > 0:
                for item in parsed_list:
                    if isinstance(item, dict) and item.get('type') == 'string' and 'data' in item:
                        # Extract the UUID from "Submitted job: <UUID>"
                        data_string = item['data']
                        if "Submitted job: " in data_string:
                            actual_flare_job_id = data_string.split("Submitted job: ")[1].strip()
                            break
        except (ValueError, SyntaxError) as e:
            logger.error(f"Error parsing flare_job_id_raw: {e}")
            # If it's not a parsable list, assume it's the actual ID or filename
            actual_flare_job_id = flare_job_id_raw

        logger.info(f"actual_flare_job_id: {actual_flare_job_id}")
        
        project_identifier_str = str(result.job.project.identifier)
        
        # Prefer the job ID from the S3 key path: <project>/results/<job_id>/...
        path_parts = result.file_path.split('/')
        job_id_from_path = path_parts[2] if len(path_parts) > 2 else actual_flare_job_id
        s3_key_job_identifier_str = job_id_from_path
        s3_key_job_identifier_app_str = s3_key_job_identifier_str + "app"

        logger.info(f"s3_key_job_identifier_str: {s3_key_job_identifier_str}")
        logger.info(f"s3_key_job_identifier_app_str: {s3_key_job_identifier_app_str}")

        # Calculate the cleaned filename for the download attribute
        cleaned_filename = result.file_path.replace(project_identifier_str, "")
        cleaned_filename = cleaned_filename.replace("/results/", "")
        cleaned_filename = cleaned_filename.replace(s3_key_job_identifier_str, "")
        cleaned_filename = cleaned_filename.replace(s3_key_job_identifier_app_str, "")
        cleaned_filename = cleaned_filename.replace("/", "")
        
        logger.info(f"cleaned_filename: {cleaned_filename}")

        # Cleaned flare_job_id for display in <h4>
        # If actual_flare_job_id is the full filename, we need to clean it
        if s3_key_job_identifier_str.endswith("_fl-client-2.__nvfl_sig.json"): # Heuristic to check if it's a filename
             cleaned_flare_job_id_display = s3_key_job_identifier_str.replace(s3_key_job_identifier_app_str, "")
        else:
             cleaned_flare_job_id_display = s3_key_job_identifier_str[:36] if len(s3_key_job_identifier_str) >= 36 else s3_key_job_identifier_str
        
        logger.info(f"cleaned_flare_job_id_display: {cleaned_flare_job_id_display}")

        file_type = os.path.splitext(result.file_path)[1].lstrip('.').lower() or 'unknown'

        prepared_results.append({
            'id': result.id,
            'file_path': result.file_path,
            'file_size': result.file_size,
            'file_type': file_type,
            'job_flare_job_id_str': actual_flare_job_id, # This is now the extracted ID or raw string
            'job_project_identifier_str': project_identifier_str,
            's3_key_job_identifier_str': s3_key_job_identifier_str, # New field for template
            's3_key_job_identifier_app_str': s3_key_job_identifier_app_str, # New field for template
            'cleaned_filename': cleaned_filename,
            'cleaned_flare_job_id_display': cleaned_flare_job_id_display,
        })


    context = {
        'segment': 'results',
        'results': prepared_results, # Pass the prepared list
        'project_identifier': current_project_uuid,
        'job_options': job_options,
    }
    return render(request, "apps/results/results.html", context)

@login_required(login_url='/users/signin/')
def download_result(request, result_id):
    try:
        result = TrainingResult.objects.get(id=result_id)
        download_url = get_s3_download_url(result.file_path)
        return redirect(download_url)
    except TrainingResult.DoesNotExist:
        return render(request, "404.html")

@login_required(login_url='/users/signin/')
def download_all_results(request, project_id):
    try:
        project = Project.objects.get(identifier=project_id)
        results = TrainingResult.objects.filter(job__project=project)
        job_filter = request.GET.get('job')
        if job_filter:
            results = results.filter(file_path__contains=f"/results/{job_filter}/")
        
        if not results.exists():
            return render(request, "404.html")

        s3_client = get_s3_client()
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for result in results:
                file_path = result.file_path
                file_name = file_path.split('/')[-1]
                
                response = s3_client.get_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=file_path)
                file_content = response['Body'].read()
                
                zip_file.writestr(file_name, file_content)

        zip_buffer.seek(0)
        response = HttpResponse(zip_buffer, content_type='application/zip')
        response['Content-Disposition'] = f'attachment; filename="{project.title}_results.zip"'
        return response

    except Project.DoesNotExist:
        return render(request, "404.html")