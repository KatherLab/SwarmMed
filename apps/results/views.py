"""
View functions for the results application.
Handles displaying training results, starting/stopping visualization tasks,
and downloading result files from S3.
"""

import io
import logging
import os
import zipfile
from urllib.parse import urlparse

from celery import current_app
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import (
    HttpResponse,
    JsonResponse,
    HttpResponseForbidden,
    HttpResponseRedirect,
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
        user_current_project = UserCurrentProject.objects.get(
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

    # Trigger a background sync task to ensure the database matches S3.
    sync_project_results.delay(current_project_uuid)
    messages.info(
        request,
        "Result synchronization started in background. Page will refresh automatically.",
    )

    # Initialize S3 client to list objects.
    s3 = get_s3_client()
    prefix = f"{project.identifier}/results/"
    paginator = s3.get_paginator("list_objects_v2")

    # We use these sets/dicts to collect unique jobs found in S3.
    job_ids_in_s3 = set()
    job_last_modified = {}
    s3_items = []

    # Paginate through S3 objects to find result files.
    for page in paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix
    ):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue

            # S3 Key format:
            # <project_uuid>/results/<job_id>/<client_name>/<filename>
            parts = key.split("/")
            if len(parts) < 4:
                continue

            job_id = parts[2]
            job_ids_in_s3.add(job_id)

            # Track the latest modification time for each job.
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

            # Synchronize this specific file into our database TrainingResult
            # model.
            try:
                # flare_job_id might contain the job_id string.
                job = TrainingJob.objects.filter(
                    project=project, flare_job_id__icontains=job_id
                ).first()

                if job:
                    tr, created = TrainingResult.objects.get_or_create(
                        job=job,
                        file_path=key,
                        defaults={"file_size": obj.get("Size", 0)},
                    )
                    if not created and tr.file_size != obj.get("Size", 0):
                        tr.file_size = obj.get("Size", 0)
                        tr.save(update_fields=["file_size"])
            except Exception as e:
                _logger.warning(f"Error syncing result for {key}: {e}")

    # Build a list of job options for the dropdown selector.
    job_options = []
    for job_id in list(job_ids_in_s3):
        db_job = TrainingJob.objects.filter(
            project=project, flare_job_id__icontains=job_id
        ).first()

        lm = job_last_modified.get(job_id)

        if db_job:
            # Prefer database timestamp for cleaner formatting.
            label = db_job.created_at.strftime("%Y-%m-%d %H:%M:%S")
            sort_time = db_job.created_at
        else:
            # Fallback to S3 timestamp.
            label = lm.strftime("%Y-%m-%d %H:%M:%S") if lm else job_id
            sort_time = lm if lm else timezone.now()

        job_options.append(
            {"value": job_id, "label": label, "last_modified": sort_time}
        )

    # Sort options so the newest job is at the top.
    job_options.sort(key=lambda x: x["last_modified"], reverse=True)

    # Handle user selection from the GET parameters.
    selected_job_id = request.GET.get("job")

    # Default to the most recent job if none selected.
    if "job" not in request.GET and job_options:
        selected_job_id = job_options[0]["value"]

    # Filter S3 items to show only those belonging to the selected job.
    if selected_job_id:
        s3_items = [
            it
            for it in s3_items
            if f"/results/{selected_job_id}/" in it["key"]
        ]

    # Prepare data for the template.
    prepared_results = []
    for item in s3_items:
        key = item["key"]
        parts = key.split("/")
        prepared_results.append(
            {
                "file_path": key,
                "file_size": item["size"],
                "file_type": os.path.splitext(key)[1].lstrip(".").lower()
                or "unknown",
                "cleaned_filename": os.path.basename(key),
                "uploaded_at": item.get("last_modified"),
                "client_name": parts[3] if len(parts) > 3 else "unknown",
            }
        )

    # Fetch details for the selected job for the header display.
    selected_job_details = None
    if selected_job_id:
        selected_job_details = TrainingJob.objects.filter(
            project=project, flare_job_id__icontains=selected_job_id
        ).first()

    context = {
        "segment": "results",
        "project": project,
        "results": prepared_results,
        "project_identifier": current_project_uuid,
        "job_options": job_options,
        "selected_job_id": selected_job_id,
        "selected_job_details": selected_job_details,
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
        job = get_object_or_404(TrainingJob, identifier=job_id)

        if job.project != project:
            return JsonResponse({"error": "Job does not belong to this project"}, status=400)

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
            return JsonResponse(
                {
                    "error": (
                        f"No visualization scripts found at {script_prefix}. "
                        "Please upload one on the project page."
                    )
                },
                status=400,
            )

        # Cancel any existing visualization tasks for this specific job to
        # avoid overlap.
        running_visualizations = ResultsVisualizationRun.objects.filter(
            project=project, job=job, status__in=["pending", "running"]
        )
        for viz in running_visualizations:
            if viz.celery_task_id:
                current_app.control.revoke(viz.celery_task_id, terminate=True)
            viz.status = "cancelled"
            viz.completed_at = timezone.now()
            viz.save()

        # Create a new run record in the database.
        visualization_run = ResultsVisualizationRun.objects.create(
            project=project, job=job, user=request.user
        )

        # Dispatch the task to Celery.
        task = run_results_visualization_task.delay(
            str(visualization_run.id), str(job.identifier)
        )
        visualization_run.celery_task_id = task.id
        visualization_run.save()

        return JsonResponse(
            {
                "success": True,
                "visualization_run_id": str(visualization_run.id),
            }
        )

    except (Project.DoesNotExist, TrainingJob.DoesNotExist):
        return JsonResponse(
            {"error": "Project or Training Job not found"}, status=404
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

        if visualization_run.status not in ["pending", "running"]:
            return JsonResponse(
                {"error": "No running visualization found."}, status=404
            )

        if visualization_run.celery_task_id:
            # Signal Celery to terminate the task process.
            current_app.control.revoke(
                visualization_run.celery_task_id, terminate=True
            )

        visualization_run.status = "cancelled"
        visualization_run.completed_at = timezone.now()
        visualization_run.save()

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
        job = get_object_or_404(TrainingJob, identifier=job_id)

        # Get the most recent run for this job.
        latest_visualization = ResultsVisualizationRun.objects.filter(
            project=job.project, job=job
        ).first()

        if not latest_visualization:
            return JsonResponse({"status": "none", "plots": []})

        # Retrieve all plots associated with this run.
        plots = list(
            ResultsVisualizationPlot.objects.filter(
                visualization_run=latest_visualization
            ).values("title", "plot_number", "image_data", "svg_data")
        )

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

    except TrainingJob.DoesNotExist:
        return JsonResponse({"error": "Training job not found"}, status=404)


@login_required
@project_membership_required
def download_result(request, result_id):
    """
    Redirects the user to a temporary S3 download URL for a specific result file.
    Logs the access for audit trails.
    """
    try:
        result = get_object_or_404(TrainingResult, id=result_id)

        log = logger.get_logger()
        log.access.info(f"User downloaded training result: {result.file_path}", file_key=result.file_path)

        download_url = get_s3_download_url(result.file_path)

        # Security: Validate the redirect URL host
        parsed_url = urlparse(download_url)
        allowed_hosts = [
            urlparse(settings.AWS_S3_ENDPOINT_URL).netloc,
            urlparse(settings.PUBLIC_URL).netloc
        ]

        # Allow configured S3 hosts or standard AWS S3 domains
        if parsed_url.netloc not in allowed_hosts and not parsed_url.netloc.endswith("amazonaws.com"):
            return HttpResponseForbidden("External URL forbidden")

        # If there is a safe referer, we prefer to stay in the app and open the
        # link (e.g. in a new tab if the frontend does that), but usually, we just
        # want to go to the download URL.
        # We use HttpResponseRedirect directly for the external URL to be explicit.
        return HttpResponseRedirect(download_url)
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
    Logs the access for audit trails.
    """
    current_project_uuid, _ = get_user_project(request)

    project = get_object_or_404(Project, identifier=current_project_uuid)

    key = request.GET.get("key", "")
    # Security check: Ensure the requested key belongs to the user's active
    # project.
    if not key or not key.startswith(f"{current_project_uuid}/results/"):
        return render(request, "404.html")

    log = logger.get_logger()
    log.access.info(f"User downloaded result file by key: {key}", file_key=key)

    download_url = get_s3_download_url(key)

    # Security: Validate the redirect URL host
    parsed_url = urlparse(download_url)
    allowed_hosts = [
        urlparse(settings.AWS_S3_ENDPOINT_URL).netloc,
        urlparse(settings.PUBLIC_URL).netloc
    ]

    # Allow configured S3 hosts or standard AWS S3 domains
    if parsed_url.netloc not in allowed_hosts and not parsed_url.netloc.endswith("amazonaws.com"):
        return HttpResponseForbidden("External URL forbidden")

    # Use HttpResponseRedirect directly for the external S3 URL.
    return HttpResponseRedirect(download_url)
