"""
View functions for the results application.
Handles displaying training results, starting/stopping visualization tasks,
and downloading result files from S3.
"""

import io
import logging
import os
import re
import zipfile
from urllib.parse import urlparse

from django.urls import reverse
from celery import current_app
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models
from django.http import (
    HttpResponse,
    JsonResponse,
    HttpResponseForbidden,
    HttpResponseRedirect,
    StreamingHttpResponse,
)
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from apps.data.utils import get_s3_client, get_s3_download_url
from apps.project.models import Project, UserCurrentProject
from apps.training.models import TrainingJob
from apps.logs import logger

from .models import (
    ResultsVisualizationPlot,
    ResultsVisualizationRun,
    TrainingResult,
)
from .tasks import run_results_visualization_task, sync_project_results

# Standard Python logger for this module.
_logger = logging.getLogger(__name__)


from ..project.decorators import project_context_required, project_membership_required

def get_user_project(request):
    """
    Helper function to retrieve the user's currently active project.

    Returns:
        (str or None, bool): (Project UUID string, Success flag)
    """
    try:
        user_current_project = UserCurrentProject.objects.select_related('project').get(
            user=request.user
        )
        if not user_current_project.project:
            return None, False
        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False


@login_required
@project_context_required
@ensure_csrf_cookie
def results(request):
    """
    The main results dashboard view.
    Synchronizes results from S3, lists available jobs, and displays result files.
    """
    current_project_uuid, _ = get_user_project(request)

    project = get_object_or_404(Project, identifier=current_project_uuid)
    log = logger.get_logger(user=request.user, project=project)

    # Trigger a background sync task to ensure the database matches S3.
    sync_project_results.delay(current_project_uuid)
    log.results.debug(f"Triggered results sync for project {current_project_uuid}")
    
    # Initialize S3 client to list objects (source of truth for existence).
    s3 = get_s3_client()
    prefix = f"{project.identifier}/results/"
    paginator = s3.get_paginator("list_objects_v2")

    job_ids_in_s3 = set()
    job_last_modified = {}
    s3_items = []

    # Paginate through S3 objects.
    try:
        for page in paginator.paginate(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue

                parts = key.split("/")
                if len(parts) < 4:
                    continue

                job_id = parts[2]
                job_ids_in_s3.add(job_id)

                last_modified = obj.get("LastModified")
                if last_modified:
                    prev = job_last_modified.get(job_id)
                    job_last_modified[job_id] = (
                        max(prev, last_modified) if prev else last_modified
                    )

                s3_items.append(
                    {
                        "key": key,
                        "size": obj.get("Size", 0),
                        "last_modified": last_modified,
                    }
                )
    except Exception as e:
        log.results.error(f"Error listing S3 objects: {e}")

    # Fetch all jobs for this project to build the dropdown options.
    all_project_jobs = TrainingJob.objects.filter(project=project).order_by('-created_at')
    
    # Match database results by file_path for efficient lookup
    db_results = {r.file_path: r for r in TrainingResult.objects.filter(job__project=project)}

    # Build a list of job options for the dropdown selector.
    job_options = []
    for job_id_s3 in list(job_ids_in_s3):
        # Match S3 job_id against flare_job_id in DB
        db_job = all_project_jobs.filter(flare_job_id__icontains=job_id_s3).first()
        lm = job_last_modified.get(job_id_s3)

        if db_job:
            label = f"{db_job.created_at.strftime('%Y-%m-%d %H:%M:%S')} ({job_id_s3[:8]})"
            sort_time = db_job.created_at
        else:
            label = lm.strftime("%Y-%m-%d %H:%M:%S") if lm else job_id_s3
            sort_time = lm if lm else timezone.now()

        job_options.append(
            {"value": job_id_s3, "label": label, "last_modified": sort_time}
        )

    # Sort options: newest first.
    job_options.sort(key=lambda x: x["last_modified"], reverse=True)

    # User selection.
    selected_job_id = request.GET.get("job")
    if "job" not in request.GET and job_options:
        selected_job_id = job_options[0]["value"]

    # Filter items for selected job.
    if selected_job_id:
        s3_items = [it for it in s3_items if f"/results/{selected_job_id}/" in it["key"]]

    # Prepare results for display, matching S3 items with DB records where possible.
    prepared_results = []
    for item in s3_items:
        key = item["key"]
        parts = key.split("/")
        db_rec = db_results.get(key)
        
        prepared_results.append(
            {
                "id": db_rec.id if db_rec else None,
                "file_path": key,
                "file_size": item["size"],
                "file_type": os.path.splitext(key)[1].lstrip(".").lower() or "unknown",
                "cleaned_filename": os.path.basename(key),
                "uploaded_at": db_rec.created_at if db_rec else item.get("last_modified"),
                "client_name": parts[3] if len(parts) > 3 else "unknown",
            }
        )

    # Sort results by time (newest first).
    prepared_results.sort(key=lambda x: x["uploaded_at"] or timezone.now(), reverse=True)

    # Selected job details for header and visualization.
    selected_job_details = None
    if selected_job_id:
        # Try to find the exact job first
        selected_job_details = all_project_jobs.filter(flare_job_id__icontains=selected_job_id).first()
    
    # Fallback: if no job selected explicitly (first visit), but jobs exist,
    # default to the latest job so the visualization box can be rendered.
    # But if user explicitly selected "All jobs" (job=""), selected_job_id will be empty string.
    if "job" not in request.GET and not selected_job_details and all_project_jobs.exists():
        selected_job_details = all_project_jobs.first()
        if selected_job_details:
            selected_job_id = selected_job_details.flare_job_id # Ensure we have a valid string for the fallback job

    context = {
        "segment": "results",
        "project": project,
        "results": prepared_results,
        "project_identifier": current_project_uuid,
        "job_options": job_options,
        "selected_job_id": selected_job_id,
        "selected_job_details": selected_job_details,
        "has_jobs": bool(job_options), # Check job_options which includes S3-only jobs
    }
    return render(request, "apps/results/results.html", context)


@login_required
@require_POST
@project_membership_required
def start_results_visualization(request, job_id):
    """
    Triggers a background task to run the project's results visualization script.
    """
    current_project_uuid, _ = get_user_project(request)

    try:
        project = get_object_or_404(Project, identifier=current_project_uuid)
        log = logger.get_logger(user=request.user, project=project)

        # 1. Try to find the job in DB by identifier (UUID) or flare_job_id.
        job = TrainingJob.objects.filter(
            models.Q(identifier=job_id) | models.Q(flare_job_id__icontains=job_id),
            project=project
        ).first()

        # flare_id is what the sandbox needs to find the files in S3.
        # If we have a DB record, use its clean flare_job_id. 
        # If not, assume job_id passed from frontend is the flare_job_id string from S3.
        flare_id = job_id
        if job:
            flare_id = job.flare_job_id
            # Clean the NVFlare job ID if it's complex.
            try:
                import ast
                parsed = ast.literal_eval(flare_id)
                if isinstance(parsed, list):
                    for item in parsed:
                        if (isinstance(item, dict) and
                                item.get('type') == 'string' and
                                'Submitted job:' in item.get('data', '')):
                            flare_id = item.get('data', '').split(':')[-1].strip()
                            break
            except (ValueError, SyntaxError):
                pass

        # Check if the project actually has a visualization script uploaded.
        script_prefix = f"{project.identifier}/code/results_visualization/"
        s3 = get_s3_client()
        response = s3.list_objects_v2(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=script_prefix
        )
        py_scripts = [
            obj["Key"]
            for obj in response.get("Contents", [])
            if obj["Key"].endswith(".py")
        ]

        if not py_scripts:
            log.results.error(f"No visualization script found for project {project.identifier}")
            return JsonResponse(
                {
                    "error": (
                        f"No visualization scripts found at {script_prefix}. "
                        "Please upload one on the project page."
                    )
                },
                status=400,
            )

        # Cancel any existing visualization tasks for this specific flare_id to
        # avoid overlap.
        running_query = ResultsVisualizationRun.objects.filter(
            project=project, status__in=["pending", "running"],
            flare_job_id=flare_id
        )

        for viz in running_query:
            if viz.celery_task_id:
                current_app.control.revoke(viz.celery_task_id, terminate=True)
            viz.status = "cancelled"
            viz.completed_at = timezone.now()
            viz.save()
            log.results.info(f"Cancelled previous results visualization run {viz.id}")

        # Create a new run record in the database.
        visualization_run = ResultsVisualizationRun.objects.create(
            project=project, job=job, flare_job_id=flare_id, user=request.user
        )

        # Dispatch the task to Celery.
        # We pass flare_id to the task so it knows which S3 folder to download.
        task = run_results_visualization_task.delay(
            str(visualization_run.id), flare_id
        )
        visualization_run.celery_task_id = task.id
        visualization_run.save()

        log.results.info(f"Started results visualization run {visualization_run.id} for job {flare_id}")

        return JsonResponse(
            {
                "success": True,
                "visualization_run_id": str(visualization_run.id),
            }
        )

    except Project.DoesNotExist:
        return JsonResponse(
            {"error": "Project not found"}, status=404
        )
    except Exception as e:
        _logger.exception("Failed to start visualization")
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@require_POST
def stop_results_visualization(request):
    """
    Stops a currently running visualization task.
    """
    run_id = request.POST.get("run_id")
    try:
        visualization_run = ResultsVisualizationRun.objects.get(
            id=run_id, user=request.user
        )
        log = logger.get_logger(user=request.user, project=visualization_run.project)

        if visualization_run.status not in ["pending", "running"]:
            log.results.debug(f"Stop visualization requested for {run_id} but status is {visualization_run.status}")
            return JsonResponse(
                {"error": "No running visualization found."}, status=404
            )

        if visualization_run.celery_task_id:
            # Signal Celery to terminate the task process.
            current_app.control.revoke(
                visualization_run.celery_task_id, terminate=True
            )
            log.results.info(f"Revoked Celery task {visualization_run.celery_task_id} for visualization run {run_id}")

        visualization_run.status = "cancelled"
        visualization_run.completed_at = timezone.now()
        visualization_run.save()
        log.results.warning(f"Results visualization run {run_id} cancelled by user.")

        return JsonResponse({"success": True})

    except ResultsVisualizationRun.DoesNotExist:
        return JsonResponse(
            {"error": "Visualization run not found"}, status=404
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@project_membership_required
def results_visualization_status(request, job_id):
    """
    API endpoint for the frontend to poll the status of a visualization run.
    """
    try:
        current_project_uuid, _ = get_user_project(request)
        project = get_object_or_404(Project, identifier=current_project_uuid)

        # Get the clean flare_id for filtering. 
        # Frontend might pass a UUID if DB record exists, or a string flare_id.
        flare_id = job_id
        job = TrainingJob.objects.filter(
            models.Q(identifier=job_id) | models.Q(flare_job_id__icontains=job_id),
            project=project
        ).first()
        
        if job:
            flare_id = job.flare_job_id
            try:
                import ast
                parsed = ast.literal_eval(flare_id)
                if isinstance(parsed, list):
                    for item in parsed:
                        if (isinstance(item, dict) and
                                item.get('type') == 'string' and
                                'Submitted job:' in item.get('data', '')):
                            flare_id = item.get('data', '').split(':')[-1].strip()
                            break
            except (ValueError, SyntaxError):
                pass

        # Get the most recent run for this flare_id.
        latest_visualization = ResultsVisualizationRun.objects.filter(
            project=project, flare_job_id=flare_id
        ).first()

        if not latest_visualization:
            return JsonResponse({"status": "none", "plots": []})

        # Retrieve all plots associated with this run.
        plots_qs = ResultsVisualizationPlot.objects.filter(
            visualization_run=latest_visualization
        )
        plots = []
        for p in plots_qs:
            plots.append({
                "title": p.title,
                "plot_number": p.plot_number,
                "image_url": reverse('results:get_visualization_plot', args=[p.id, 'image']) if p.image_data else None,
                "svg_url": reverse('results:get_visualization_plot', args=[p.id, 'svg']) if p.svg_data else None,
            })

        return JsonResponse(
            {
                "run_id": latest_visualization.id,
                "status": latest_visualization.status,
                "success": latest_visualization.success,
                "output": latest_visualization.output,
                "error_message": latest_visualization.error_message,
                "plots": plots,
                "started_at": latest_visualization.started_at,
                "completed_at": latest_visualization.completed_at,
            }
        )

    except Project.DoesNotExist:
        return JsonResponse({"error": "Project not found"}, status=404)


def _proxy_s3_download(key, filename):
    """
    Helper to proxy a file download from S3 through Django using StreamingHttpResponse.
    """
    s3 = get_s3_client()
    bucket = settings.AWS_STORAGE_BUCKET_NAME
    
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        
        def stream_content():
            for chunk in obj['Body'].iter_chunks(chunk_size=1024*1024): # 1MB chunks
                yield chunk
        
        # Force application/octet-stream to prevent Nginx from gzipping the content.
        # If Nginx gzips, it changes the content length but might not strip the header
        # when buffering is disabled, leading to "Network Error" in Chrome.
        response = StreamingHttpResponse(
            stream_content(),
            content_type='application/octet-stream'
        )
        # Set Content-Length if available (helps Chrome show progress and verify completion)
        if obj.get('ContentLength'):
            response['Content-Length'] = obj['ContentLength']

        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        # Disable Nginx buffering for this stream
        response['X-Accel-Buffering'] = 'no'
        return response
    except Exception as e:
        _logger.error(f"Failed to proxy S3 download for {key}: {e}")
        return HttpResponse("File download failed", status=500)


@login_required
@project_membership_required
def download_result(request, result_id):
    """
    Serves a result file by proxying the download through Django.
    This avoids issues with self-signed certificates or inaccessible S3 hosts (e.g. Docker network).
    """
    try:
        result = get_object_or_404(TrainingResult, id=result_id)

        log = logger.get_logger()
        log.access.info(f"User downloading training result: {result.file_path}", file_key=result.file_path)

        # Direct proxying is more reliable for local/self-hosted setups
        return _proxy_s3_download(result.file_path, os.path.basename(result.file_path))
        
    except TrainingResult.DoesNotExist:
        return render(request, "404.html")


@login_required
@project_membership_required
def download_all_results(request, project_id):
    """
    Gathers all result files for a project (or job) and provides them as a ZIP archive.
    Logs the bulk access event.
    """
    try:
        project = get_object_or_404(Project, identifier=project_id)

        s3 = get_s3_client()
        job_filter = request.GET.get("job")
        prefix = f"{project.identifier}/results/"

        # Identify all files belonging to this project/job.
        keys = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                if job_filter and f"/results/{job_filter}/" not in key:
                    continue
                keys.append(key)

        if not keys:
            return render(request, "404.html")

        log = logger.get_logger()
        log.access.info(f"User downloaded all results for project {project.identifier} (Job: {job_filter})", project_id=project.identifier)

        # Create the ZIP archive in memory.
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(
            zip_buffer, "w", zipfile.ZIP_DEFLATED
        ) as zip_file:
            for key in keys:
                # Remove project prefix from paths inside the ZIP for cleaner
                # structure.
                rel_name = (
                    key[len(prefix) :] if key.startswith(prefix) else key
                )
                obj = s3.get_object(
                    Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=key
                )
                zip_file.writestr(rel_name, obj["Body"].read())

        zip_buffer.seek(0)
        response = HttpResponse(zip_buffer, content_type="application/zip")

        # Construct filename based on context.
        base_name = (
            f"{project.title}_{job_filter}_results.zip"
            if job_filter
            else f"{project.title}_results.zip"
        )
        response["Content-Disposition"] = f'attachment; filename="{base_name}"'
        return response

    except Project.DoesNotExist:
        return render(request, "404.html")


@login_required
@project_context_required
def download_result_by_key(request):
    """
    Downloads a result file using its S3 key (passed as a GET parameter).
    Used as a fallback for results not yet indexed in the database.
    """
    current_project_uuid, _ = get_user_project(request)

    project = get_object_or_404(Project, identifier=current_project_uuid)

    key = request.GET.get("key", "")
    # Security check: Ensure the requested key belongs to the user's active
    # project.
    if not key or not key.startswith(f"{current_project_uuid}/results/"):
        return render(request, "404.html")

    log = logger.get_logger()
    log.access.info(f"User downloading result file by key: {key}", file_key=key)

    # Direct proxying is more reliable for local/self-hosted setups
    return _proxy_s3_download(key, os.path.basename(key))


@login_required
@project_membership_required
def get_visualization_plot(request, plot_id, plot_type):
    """
    Proxies a visualization plot image from S3 through Django.
    """
    plot = get_object_or_404(ResultsVisualizationPlot, id=plot_id)
    
    key = None
    filename = f"plot_{plot.plot_number}"
    
    if plot_type == 'image' and plot.image_data:
        key = plot.image_data.name
        filename += ".png"
    elif plot_type == 'svg' and plot.svg_data:
        key = plot.svg_data.name
        filename += ".svg"
        
    if not key:
        return HttpResponse("Plot data not found", status=404)
        
    response = _proxy_s3_download(key, filename)
    # Ensure plots are displayed inline
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response
