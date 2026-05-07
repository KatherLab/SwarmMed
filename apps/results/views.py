"""View functions for the results application.
Handles displaying training results, starting/stopping visualization tasks,
and downloading result files from S3.
"""

import io
import logging
import os
import re
import zipfile

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import (
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from common.utils import get_s3_client
from logs import logger
from project.decorators import (
    project_context_required,
    project_membership_required,
)
from project.models import Project, UserCurrentProject

from . import services as results_services
from .models import TrainingResult

# Standard Python logger for this module.
_logger = logging.getLogger(__name__)


def get_user_project(request):
    """Helper function to retrieve the user's currently active project.

    Returns:
        (str or None, bool): (Project UUID string, Success flag)
    """
    try:
        user_current_project = UserCurrentProject.objects.select_related(
            "project"
        ).get(user=request.user)
        if not user_current_project.project:
            return None, False
        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False


@login_required
@project_context_required
@ensure_csrf_cookie
def results(request):
    """The main results dashboard view.
    Synchronizes results from S3, lists available jobs, and displays result files.
    """
    current_project_uuid, _ = get_user_project(request)
    project = get_object_or_404(Project, identifier=current_project_uuid)
    context = results_services.build_results_dashboard_context(
        project,
        selected_job_id=request.GET.get("job"),
        default_to_latest="job" not in request.GET,
    )
    return render(request, "apps/results/results.html", context)


@login_required
@require_POST
@project_membership_required
def start_results_visualization(request, job_id):
    """Triggers a background task to run the project's results visualization script."""
    current_project_uuid, _ = get_user_project(request)

    try:
        project = get_object_or_404(Project, identifier=current_project_uuid)
        visualization_run = results_services.start_results_visualization(
            project=project, user=request.user, job_identifier=job_id
        )

        return JsonResponse(
            {
                "success": True,
                "visualization_run_id": str(visualization_run.id),
            }
        )

    except Project.DoesNotExist:
        return JsonResponse({"error": "Project not found"}, status=404)
    except Exception as e:
        _logger.exception("Failed to start visualization")
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@require_POST
def stop_results_visualization(request):
    """Stops a currently running visualization task."""
    run_id = request.POST.get("run_id")
    try:
        results_services.stop_results_visualization(
            user=request.user, run_id=run_id
        )

        return JsonResponse({"success": True})

    except LookupError:
        return JsonResponse(
            {"error": "Visualization run not found"}, status=404
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@project_membership_required
def results_visualization_status(request, job_id):
    """API endpoint for the frontend to poll the status of a visualization run."""
    try:
        current_project_uuid, _ = get_user_project(request)
        project = get_object_or_404(Project, identifier=current_project_uuid)
        latest_visualization = results_services.get_results_visualization_run(
            project, job_identifier=job_id
        )

        if not latest_visualization:
            return JsonResponse({"status": "none", "plots": []})

        serialized = results_services.serialize_results_visualization_run(
            latest_visualization
        )
        serialized["run_id"] = latest_visualization.id
        serialized["plots"] = [
            {
                "title": plot["title"],
                "plot_number": plot["plot_number"],
                "image_url": (
                    reverse(
                        "results:get_visualization_plot",
                        args=[plot["identifier"], "image"],
                    )
                    if plot["image_key"]
                    else None
                ),
                "svg_url": (
                    reverse(
                        "results:get_visualization_plot",
                        args=[plot["identifier"], "svg"],
                    )
                    if plot["svg_key"]
                    else None
                ),
            }
            for plot in serialized["plots"]
        ]

        return JsonResponse(serialized)

    except Project.DoesNotExist:
        return JsonResponse({"error": "Project not found"}, status=404)


def _proxy_s3_download(request, key, filename):
    """Helper to proxy a file download from S3 through Django using StreamingHttpResponse."""
    s3 = get_s3_client()
    bucket = settings.AWS_STORAGE_BUCKET_NAME

    try:
        # Fetch stable metadata up-front (size/type). We'll stream with get_object calls
        # so we can resume using Range if the upstream connection drops mid-transfer.
        head = s3.head_object(Bucket=bucket, Key=key)
        total_size = head.get("ContentLength")

        range_header = request.META.get("HTTP_RANGE")
        range_start = 0
        range_end = (
            (total_size - 1)
            if isinstance(total_size, int) and total_size > 0
            else None
        )
        status_code = 200

        if range_header and isinstance(total_size, int) and total_size > 0:
            match = re.match(r"^bytes=(\d+)-(\d*)$", range_header.strip())
            if match:
                requested_start = int(match.group(1))
                requested_end = (
                    int(match.group(2)) if match.group(2) else (total_size - 1)
                )
                if 0 <= requested_start < total_size:
                    range_start = requested_start
                    range_end = min(requested_end, total_size - 1)
                    status_code = 206

        target_end_exclusive = None
        if range_end is not None:
            target_end_exclusive = range_end + 1
        elif isinstance(total_size, int) and total_size > 0:
            target_end_exclusive = total_size

        def stream_content():
            bytes_sent = range_start
            attempts = 0
            max_attempts = 5

            while True:
                had_progress = False
                try:
                    get_kwargs = {"Bucket": bucket, "Key": key}
                    if range_end is not None:
                        get_kwargs["Range"] = f"bytes={bytes_sent}-{range_end}"
                    elif bytes_sent:
                        get_kwargs["Range"] = f"bytes={bytes_sent}-"

                    obj = s3.get_object(**get_kwargs)

                    for chunk in obj["Body"].iter_chunks(
                        chunk_size=1024 * 1024
                    ):  # 1MB
                        if not chunk:
                            continue
                        had_progress = True
                        bytes_sent += len(chunk)
                        yield chunk

                        # Stop exactly at the requested range end (or full size).
                        if (
                            target_end_exclusive is not None
                            and bytes_sent >= target_end_exclusive
                        ):
                            break

                    # Completed normally.
                    if (
                        target_end_exclusive is None
                        or bytes_sent >= target_end_exclusive
                    ):
                        break

                    # Upstream ended early; retry with Range.
                    attempts += 1
                    if attempts >= max_attempts:
                        break
                except Exception:
                    # Retry from the last successfully yielded byte.
                    attempts += 1
                    if attempts >= max_attempts:
                        break
                finally:
                    if had_progress:
                        attempts = 0

        # Force application/octet-stream to prevent Nginx from gzipping the content.
        # If Nginx gzips, it changes the content length but might not strip the header
        # when buffering is disabled, leading to "Network Error" in Chrome.
        response = StreamingHttpResponse(
            stream_content(),
            content_type="application/octet-stream",
            status=status_code,
        )
        response["Accept-Ranges"] = "bytes"

        # Content-Length + Content-Range for partial responses
        if isinstance(total_size, int) and total_size > 0:
            if status_code == 206 and range_end is not None:
                response["Content-Length"] = str(range_end - range_start + 1)
                response["Content-Range"] = (
                    f"bytes {range_start}-{range_end}/{total_size}"
                )
            else:
                response["Content-Length"] = str(total_size)

        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        # Prevent intermediaries from modifying the payload.
        response["Cache-Control"] = "no-transform"

        # Disable Nginx buffering for this stream
        response["X-Accel-Buffering"] = "no"
        return response
    except Exception as e:
        _logger.error(f"Failed to proxy S3 download for {key}: {e}")
        return HttpResponse("File download failed", status=500)


def _proxy_s3_download_file(key, filename):
    """Fully buffer the S3 object and return a plain HttpResponse to avoid any
    streaming/range/chunked edge cases in Chrome/self-signed TLS setups.
    """
    s3 = get_s3_client()
    bucket = settings.AWS_STORAGE_BUCKET_NAME

    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        data = obj["Body"].read()
        content_type = obj.get("ContentType", "application/octet-stream")
        size = len(data)

        response = HttpResponse(data, content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Content-Length"] = str(size)
        response["Cache-Control"] = "no-transform"
        return response
    except Exception as e:
        _logger.error(
            f"Failed to download S3 file {key} via buffered HttpResponse: {e}"
        )
        return HttpResponse("File download failed", status=500)


@login_required
@project_membership_required
def download_result(request, result_id):
    """Serves a result file by proxying the download through Django.
    This avoids issues with self-signed certificates or inaccessible S3 hosts (e.g. Docker network).
    """
    try:
        current_project_uuid, _ = get_user_project(request)
        if not current_project_uuid:
            return render(request, "404.html")

        result = get_object_or_404(
            TrainingResult,
            id=result_id,
            job__project__identifier=current_project_uuid,
        )

        log = logger.get_logger()
        log.access.info(
            f"User exported training result: {result.file_path}",
            file_key=result.file_path,
        )

        # Direct proxying is more reliable for local/self-hosted setups
        return _proxy_s3_download_file(
            result.file_path, os.path.basename(result.file_path)
        )

    except TrainingResult.DoesNotExist:
        return render(request, "404.html")


@login_required
@project_membership_required
def download_all_results(request, project_id):
    """Gathers all result files for a project (or job) and provides them as a ZIP archive.
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
        log.access.info(
            f"User exported all results for project {project.identifier} (Job: {job_filter})",
            project_id=project.identifier,
        )

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
    """Downloads a result file using its S3 key (passed as a GET parameter).
    Used as a fallback for results not yet indexed in the database.
    """
    current_project_uuid, _ = get_user_project(request)

    get_object_or_404(Project, identifier=current_project_uuid)

    key = request.GET.get("key", "")
    # Security check: Ensure the requested key belongs to the user's active
    # project.
    if not key or not key.startswith(f"{current_project_uuid}/results/"):
        return render(request, "404.html")

    log = logger.get_logger()
    log.access.info(
        f"User exported result file by key: {key}", file_key=key
    )

    # Direct proxying is more reliable for local/self-hosted setups
    return _proxy_s3_download_file(key, os.path.basename(key))


@login_required
@project_membership_required
def get_visualization_plot(request, plot_id, plot_type):
    """Proxies a visualization plot image from S3 through Django."""
    current_project_uuid, _ = get_user_project(request)
    if not current_project_uuid:
        return HttpResponse("Plot data not found", status=404)

    project = get_object_or_404(Project, identifier=current_project_uuid)
    plot = results_services.resolve_results_visualization_plot(project, plot_id)
    if plot is None:
        return HttpResponse("Plot data not found", status=404)

    key = None
    filename = f"plot_{plot.plot_number}"

    if plot_type == "image" and plot.image_data:
        key = plot.image_data.name
        filename += ".png"
    elif plot_type == "svg" and plot.svg_data:
        key = plot.svg_data.name
        filename += ".svg"

    if not key:
        return HttpResponse("Plot data not found", status=404)

    response = _proxy_s3_download_file(key, filename)
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response
