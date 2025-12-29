from django.conf import settings
import os
import logging
import ast # Import the ast module

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST
from django.utils import timezone
from celery import current_app

from apps.project.models import Project, UserCurrentProject
from apps.training.models import TrainingJob
from .models import TrainingResult, ResultsVisualizationRun, ResultsVisualizationPlot
from .tasks import sync_project_results, run_results_visualization_task
from apps.data.utils import get_s3_download_url, get_s3_client
import zipfile
import io

logger = logging.getLogger(__name__)

def get_user_project(request):
    """
    Get the current user's active project identifier.
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
    
    sync_project_results.delay(current_project_uuid)
    messages.info(request, "Result synchronization has been started in the background. The page will refresh automatically.")

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
            
            job = None
            try:
                # This logic is complex, try to find the job by flare_job_id
                job = TrainingJob.objects.filter(project=project, flare_job_id__contains=job_id).first()
            except TrainingJob.DoesNotExist:
                job = None
            
            if job:
                tr, created = TrainingResult.objects.get_or_create(
                    job=job,
                    file_path=key,
                    defaults={'file_size': obj.get('Size', 0)}
                )
                if not created and tr.file_size != obj.get('Size', 0):
                    tr.file_size = obj.get('Size', 0)
                    tr.save(update_fields=['file_size'])

    # Build job options
    job_options = []
    for job_id in list(job_ids_in_s3):
        # Try to find the matching job in the database to get its real timestamp
        db_job = TrainingJob.objects.filter(project=project, flare_job_id__icontains=job_id).first()
        
        lm = job_last_modified.get(job_id)
        
        if db_job:
            # Use the actual job creation time from DB for the label
            label = db_job.created_at.strftime('%Y-%m-%d %H:%M:%S')
            sort_time = db_job.created_at
        else:
            # Fallback to S3 upload time if no DB record matches
            label = lm.strftime('%Y-%m-%d %H:%M:%S') if lm else job_id
            sort_time = lm if lm else timezone.now()
            
        job_options.append({
            'value': job_id, 
            'label': label, 
            'last_modified': sort_time
        })
    
    # Sort job options by actual job time descending (newest first)
    job_options.sort(key=lambda x: x['last_modified'], reverse=True)
    
    # Get selection from GET parameter
    selected_job_id = request.GET.get('job')
    
    # Default to latest job if no 'job' parameter is provided at all
    if 'job' not in request.GET and job_options:
        selected_job_id = job_options[0]['value']
    
    # If a specific job is selected (not empty), filter items
    if selected_job_id:
        s3_items = [it for it in s3_items if f"/results/{selected_job_id}/" in it['key']]
    # If selected_job_id is an empty string (from "All jobs"), we don't filter, showing everything.

    prepared_results = []
    for item in s3_items:
        key = item['key']
        size = item['size']
        last_modified = item.get('last_modified')
        cleaned_filename = os.path.basename(key)
        file_type = os.path.splitext(key)[1].lstrip('.').lower() or 'unknown'
        
        # Extract client name from path: <project>/results/<job>/<client>/<file>
        parts = key.split('/')
        client_name = parts[3] if len(parts) > 3 else 'unknown'
        
        prepared_results.append({
            'file_path': key,
            'file_size': size,
            'file_type': file_type,
            'cleaned_filename': cleaned_filename,
            'uploaded_at': last_modified,
            'client_name': client_name,
        })
    
    selected_job_details = None
    if selected_job_id:
        try:
            # Again, complex lookup
            selected_job_details = TrainingJob.objects.filter(project=project, flare_job_id__contains=selected_job_id).first()
        except TrainingJob.DoesNotExist:
            selected_job_details = None

    context = {
        'segment': 'results',
        'results': prepared_results,
        'project_identifier': current_project_uuid,
        'job_options': job_options,
        'selected_job_id': selected_job_id,
        'selected_job_details': selected_job_details,
    }
    return render(request, "apps/results/results.html", context)


@login_required(login_url='/users/signin/')
@require_POST
def start_results_visualization(request, job_id):
    """Start a results visualization run."""
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({'error': 'No project selected'}, status=400)
    
    try:
        project = Project.objects.get(identifier=current_project_uuid)
        job = TrainingJob.objects.get(identifier=job_id)

        # Basic check for script existence - look for any .py file in the folder
        script_prefix = f"{project.identifier}/code/results_visualization/"
        s3 = get_s3_client()
        response = s3.list_objects_v2(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=script_prefix)
        py_scripts = [obj['Key'] for obj in response.get('Contents', []) if obj['Key'].endswith('.py')]
        
        if not py_scripts:
            return JsonResponse({'error': f'No results visualization scripts (.py) found at {script_prefix}. Please upload one on the project page.'}, status=400)
                
        # Cancel any running visualization for this project
        running_visualizations = ResultsVisualizationRun.objects.filter(
            project=project,
            status__in=['pending', 'running']
        )
        for viz in running_visualizations:
            if viz.celery_task_id:
                current_app.control.revoke(viz.celery_task_id, terminate=True)
            viz.status = 'cancelled'
            viz.completed_at = timezone.now()
            viz.save()
            
        visualization_run = ResultsVisualizationRun.objects.create(
            project=project,
            user=request.user
        )
        
        task = run_results_visualization_task.delay(str(visualization_run.id), str(job.identifier))
        visualization_run.celery_task_id = task.id
        visualization_run.save()
        
        return JsonResponse({
            'success': True,
            'visualization_run_id': str(visualization_run.id)
        })
        
    except (Project.DoesNotExist, TrainingJob.DoesNotExist):
        return JsonResponse({'error': 'Project or Training Job not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@login_required(login_url='/users/signin/')
@require_POST  
def stop_results_visualization(request):
    """Stop the currently running results visualization."""
    run_id = request.POST.get('run_id')
    try:
        visualization_run = ResultsVisualizationRun.objects.get(id=run_id, user=request.user)
        
        if visualization_run.status not in ['pending', 'running']:
            return JsonResponse({'error': 'No running visualization found to stop.'}, status=404)

        if visualization_run.celery_task_id:
            current_app.control.revoke(visualization_run.celery_task_id, terminate=True)
        
        visualization_run.status = 'cancelled'
        visualization_run.completed_at = timezone.now()
        visualization_run.save()
        
        return JsonResponse({'success': True})
        
    except ResultsVisualizationRun.DoesNotExist:
        return JsonResponse({'error': 'Visualization run not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required(login_url='/users/signin/')
def results_visualization_status(request, job_id):
    """Get the current results visualization status for a job."""
    try:
        job = TrainingJob.objects.get(identifier=job_id)
        # Get latest visualization run for this job's project
        latest_visualization = ResultsVisualizationRun.objects.filter(project=job.project).first()
        
        if not latest_visualization:
            return JsonResponse({'status': 'none', 'plots': []})
        
        plots = list(ResultsVisualizationPlot.objects.filter(
            visualization_run=latest_visualization
        ).values('title', 'plot_number', 'image_data'))
        
        return JsonResponse({
            'run_id': latest_visualization.id,
            'status': latest_visualization.status,
            'success': latest_visualization.success,
            'output': latest_visualization.output,
            'error_message': latest_visualization.error_message,
            'plots': plots,
            'started_at': latest_visualization.started_at,
            'completed_at': latest_visualization.completed_at
        })
        
    except TrainingJob.DoesNotExist:
        return JsonResponse({'error': 'Training job not found'}, status=404)


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
