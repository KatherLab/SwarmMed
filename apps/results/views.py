from django.conf import settings
import os

from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from apps.project.models import Project, UserCurrentProject
from apps.training.models import TrainingJob
from .models import TrainingResult
from apps.data.utils import get_s3_download_url, get_s3_client
from django.http import HttpResponse
import zipfile
import io

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

    # Sync S3 objects under <project>/results/<flare_job_id>/ to DB if missing
    s3 = get_s3_client()
    prefix = f"{project.identifier}/results/"
    paginator = s3.get_paginator('list_objects_v2')
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
            try:
                job = TrainingJob.objects.get(project=project, flare_job_id=job_id)
            except TrainingJob.DoesNotExist:
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

    training_results = TrainingResult.objects.filter(job__project=project)

    context = {
        'segment': 'results',
        'results': training_results,
        'project_identifier': current_project_uuid,
    }
    return render(request, "apps/results/results.html", context)

@login_required(login_url='/users/signin/')
def sync_results(request):
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return render(request, "apps/results/no_project_selected.html", {"segment": "results"})

    project = Project.objects.get(identifier=current_project_uuid)

    uploaded = 0
    # For each job in this project, try to upload local workspace artifacts to S3
    for job in TrainingJob.objects.filter(project=project):
        try:
            project_name = project.title.replace(' ', '_')
            workspace_root = os.path.join(
                '/app', 'workspaces', str(project.identifier), str(job.network.identifier),
                'workspace', project_name, 'prod_00'
            )
            if not os.path.isdir(workspace_root):
                continue
            # Upload everything under prod_00 except startup directory
            for root, dirs, files in os.walk(workspace_root):
                if 'startup' in dirs:
                    dirs.remove('startup')
                for f in files:
                    local_path = os.path.join(root, f)
                    rel = os.path.relpath(local_path, workspace_root)
                    key = f"{project.identifier}/results/{job.flare_job_id}/{rel}"
                    try:
                        s3 = get_s3_client()
                        s3.upload_file(local_path, settings.AWS_STORAGE_BUCKET_NAME, key)
                        uploaded += 1
                    except Exception:
                        continue
        except Exception:
            continue

    if uploaded:
        messages.success(request, f"Synced {uploaded} files to Minio.")
    else:
        messages.info(request, "No local result files found to sync.")

    return redirect('results')


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
        response['Content-Disposition'] = f'attachment; filename="{project.name}_results.zip"'
        return response

    except Project.DoesNotExist:
        return render(request, "404.html")