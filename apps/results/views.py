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
    job_last_modified = {}
    s3_items = []
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
            last_modified = obj.get('LastModified')
            if last_modified:
                prev = job_last_modified.get(job_id)
                job_last_modified[job_id] = max(prev, last_modified) if prev else last_modified
            s3_items.append({'key': key, 'size': obj.get('Size', 0), 'last_modified': last_modified})
            # Also try to upsert into DB when possible (non-blocking display)
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
            if job:
                tr, created = TrainingResult.objects.get_or_create(
                    job=job,
                    file_path=key,
                    defaults={'file_size': obj.get('Size', 0)}
                )
                if not created and tr.file_size != obj.get('Size', 0):
                    tr.file_size = obj.get('Size', 0)
                    tr.save(update_fields=['file_size'])

    selected_job = request.GET.get('job')
    if selected_job:
        s3_items = [it for it in s3_items if f"/results/{selected_job}/" in it['key']]

    # Prepare results for template directly from S3
    prepared_results = []
    job_options_seen = set(job_ids_in_s3)  # ensure all appear
    job_options = []
    for job_id in sorted(job_ids_in_s3):
        lm = job_last_modified.get(job_id)
        label = lm.strftime('%Y-%m-%d %H:%M:%S') if lm else job_id[:36]
        job_options.append({'value': job_id, 'label': label})
    for item in s3_items:
        key = item['key']
        size = item['size']
        last_modified = item.get('last_modified')
        parts = key.split('/')
        project_identifier_str = parts[0]
        s3_key_job_identifier_str = parts[2]
        s3_key_job_identifier_app_str = s3_key_job_identifier_str + 'app'
        cleaned_filename = key.replace(project_identifier_str, "").replace("/results/", "").replace(s3_key_job_identifier_str, "").replace(s3_key_job_identifier_app_str, "").replace("/", "")
        file_type = os.path.splitext(key)[1].lstrip('.').lower() or 'unknown'
        prepared_results.append({
            'id': None,
            'file_path': key,
            'file_size': size,
            'file_type': file_type,
            'job_project_identifier_str': project_identifier_str,
            's3_key_job_identifier_str': s3_key_job_identifier_str,
            's3_key_job_identifier_app_str': s3_key_job_identifier_app_str,
            'cleaned_filename': cleaned_filename,
            'uploaded_at': last_modified,
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
        s3 = get_s3_client()
        job_filter = request.GET.get('job')
        prefix = f"{project.identifier}/results/"

        keys = []
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('/'):
                    continue
                if job_filter and f"/results/{job_filter}/" not in key:
                    continue
                keys.append(key)

        if not keys:
            return render(request, "404.html")

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for key in keys:
                rel_name = key[len(prefix):] if key.startswith(prefix) else key
                obj = s3.get_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key)
                zip_file.writestr(rel_name, obj['Body'].read())

        zip_buffer.seek(0)
        response = HttpResponse(zip_buffer, content_type='application/zip')
        base_name = f"{project.title}_{job_filter}_results.zip" if job_filter else f"{project.title}_results.zip"
        response['Content-Disposition'] = f'attachment; filename="{base_name}"'
        return response

    except Project.DoesNotExist:
        return render(request, "404.html")
















































@login_required(login_url='/users/signin/')
def download_result_by_key(request):
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return render(request, "apps/results/no_project_selected.html", {"segment": "results"})
    key = request.GET.get('key', '')
    if not key or not key.startswith(f"{current_project_uuid}/results/"):
        return render(request, "404.html")
    download_url = get_s3_download_url(key)
    return redirect(download_url)
