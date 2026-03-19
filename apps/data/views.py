"""Views for the data app.
Handles data management, file uploads, folder navigation,
and the triggering/monitoring of validation and visualization runs.
"""

import json
import os
import re
import secrets

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.http import (
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from celery import current_app
from common.utils import format_size, get_s3_client, get_safe_referer
from logs import logger
from project.decorators import (
    project_context_required,
    project_membership_required,
)
from project.models import Project, UserCurrentProject

from .models import (
    ValidationCheck,
    ValidationRun,
    VisualizationPlot,
    VisualizationRun,
)
from .tasks import run_validation_task, run_visualization_task
from .utils import (
    delete_s3_folder,
    delete_s3_object,
    get_column_prefixes,
    get_storage_stats,
    list_s3_folder,
    rename_s3_folder,
    rename_s3_object,
)


def get_user_project(request):
    """Gets the current user's active project identifier.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        tuple: (project_uuid_string, is_valid_boolean)
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


def get_project_manifest(request):
    """API endpoint that returns a signed manifest of all data files for a project.

    Authenticates via either standard Django session or a project-specific secret.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        JsonResponse: A JSON response containing the manifest of files and signed URLs.
    """
    project_id = request.GET.get("project_id")
    provided_secret = request.headers.get("X-Manifest-Secret")
    
    if not project_id:
        return JsonResponse({"error": "Missing project_id"}, status=400)

    # 1. Fetch Project and Check Authentication
    project = get_object_or_404(Project, identifier=project_id)
    is_authenticated = False
    
    # Mode A: Container authentication via project-specific secret
    if provided_secret and project.secret and secrets.compare_digest(provided_secret, project.secret):
        is_authenticated = True
    # Mode B: User session authentication
    elif request.user.is_authenticated:
        current_project_uuid, _ = get_user_project(request)
        if str(project.identifier) == current_project_uuid:
            is_authenticated = True
            
    if not is_authenticated:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    # 2. Build the manifest from S3
    from common.utils import get_internal_s3_download_url, get_s3_client
    
    try:
        s3 = get_s3_client()
        bucket = settings.AWS_STORAGE_BUCKET_NAME
        prefix = f"{project_id}/data/"
        
        paginator = s3.get_paginator("list_objects_v2")
        manifest = {}
        
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key or key.endswith("/"):
                    continue
                
                # Create a relative path for the manifest keys
                rel_path = key[len(prefix):]
                # Generate a signed URL that is valid for 24 hours
                manifest[rel_path] = get_internal_s3_download_url(key, expires=86400)
                
        return JsonResponse(manifest)
    except Exception as e:
        log = logger.get_logger()
        log.data.error(f"Manifest generation failed for project {project_id}: {e}")
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@project_context_required
def data(request):
    """Displays the main overview page for project data.

    Calculates and shows general statistics such as total storage size,
    folder count, and file count for the current project.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: The rendered data overview page.
    """
    current_project_uuid, _ = get_user_project(request)

    project = get_object_or_404(Project, identifier=current_project_uuid)
    root_path = f"{current_project_uuid}/data/"

    # Calculate storage stats for this specific project
    try:
        total_size, folder_count, file_count = get_storage_stats(root_path)
    except Exception as e:
        # If statistics cannot be retrieved (e.g. MinIO error), use defaults
        # and log the issue.
        import logging

        logging.getLogger("app").warning(f"Error getting storage stats: {e}")
        total_size, folder_count, file_count = 0, 0, 0

    formatted_size = format_size(total_size)

    context = {
        "segment": "data",
        "project": project,
        "folder_count": folder_count,
        "file_count": file_count,
        "space_used": formatted_size,
    }
    return render(request, "apps/data/data.html", context)


@login_required
@project_context_required
def upload_files(request):
    """Handles multi-file and folder uploads to project storage.

    Preserves the relative directory structure provided by the browser and
    validates file extensions and paths for security.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: A response indicating the outcome of the upload process.
    """
    current_project_uuid, _ = get_user_project(request)

    log = logger.get_logger()

    if request.method == "POST":
        # Get the list of files from the form
        files = request.FILES.getlist("file_field")

        # 'directories' is a JSON map of filename keys to their relative paths
        directories_json = request.POST.get("directories", "{}")
        try:
            directories = json.loads(directories_json)
        except json.JSONDecodeError:
            return HttpResponse("Invalid directory data", status=400)

        # User can specify a specific folder to upload into
        destination_folder = request.POST.get("destination_folder", "")

        # Security: Sanitize destination_folder
        destination_folder = os.path.normpath(destination_folder).lstrip(
            os.path.sep + (os.path.altsep or "")
        )
        if destination_folder == "." or not destination_folder:
            destination_folder = ""
        elif destination_folder.startswith(".."):
            log.data.warning(
                f"Blocked upload with malicious destination folder: {destination_folder}"
            )
            return HttpResponse("Invalid destination folder", status=400)

        root_path = f"{current_project_uuid}/data/"
        full_destination = os.path.join(root_path, destination_folder).replace(
            "\\", "/"
        )
        if not full_destination.endswith("/"):
            full_destination += "/"

        # Security: Allowed file extensions
        ALLOWED_EXTENSIONS = {
            ".csv",
            ".txt",
            ".json",
            ".parquet",
            ".npy",
            ".npz",
            ".h5",
            ".pt",
            ".pth",
            ".dcm",
            ".nii",
            ".nii.gz",
            ".jpg",
            ".jpeg",
            ".png",
            ".bmp",
            ".gif",
            ".pdf",
        }

        for idx, file in enumerate(files):
            # Basic security check: Validate file extension
            _, ext = os.path.splitext(file.name)
            if ext.lower() not in ALLOWED_EXTENSIONS:
                log.data.warning(
                    f"Blocked upload of disallowed file type: {file.name}"
                )
                continue

            # We use an index-based key to match the directory map
            key = f"{file.name}_{idx}"
            rel_path = directories.get(key, file.name)

            # Security Check: Prevent path traversal
            clean_rel_path = os.path.normpath(rel_path).lstrip(
                os.path.sep + (os.path.altsep or "")
            )
            if clean_rel_path.startswith("..") or os.path.isabs(
                clean_rel_path
            ):
                log.data.warning(
                    f"Blocked upload with path traversal attempt: {rel_path}"
                )
                continue

            # Combine paths and ensure forward slashes for S3 compatibility
            save_path = os.path.join(full_destination, clean_rel_path).replace(
                "\\", "/"
            )

            # Save the file to S3
            default_storage.save(save_path, file)
            
            # Invalidate caches for this path
            from .utils import invalidate_s3_caches
            invalidate_s3_caches(save_path)

        log.data.info(f"Files uploaded to {full_destination} successfully.")
        return HttpResponse("Files uploaded with folder structure preserved!")

    return render(request, "apps/data/upload.html", {"segment": "data"})


@login_required
@project_context_required
def list_files(request):
    """Displays a file browser interface with a multi-column layout.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: The rendered file browser page.
    """
    current_project_uuid, _ = get_user_project(request)

    root_path = f"{current_project_uuid}/data/"
    user_prefix = request.GET.get("prefix", "")

    log = logger.get_logger()
    log.access.info(
        f"User listed files in prefix: {user_prefix or '(root)'}",
        prefix=user_prefix,
    )

    # Calculate full S3 path
    # Generate prefixes for the column-based view (breadcrumb style)
    column_prefixes = get_column_prefixes(user_prefix)
    columns = []

    for col_prefix in column_prefixes:
        # Full path for S3 listing
        full_col_prefix = root_path + col_prefix if col_prefix else root_path

        # Get lists of folders and files from S3
        folders, files = list_s3_folder(full_col_prefix)

        processed_folders = []
        for folder in folders:
            if not folder.startswith(root_path):
                continue

            # Extract just the folder name for display
            folder_name = folder[len(full_col_prefix):-1]
            # Path relative to project root for navigation links
            relative_folder_path = folder[len(root_path):]

            processed_folders.append(
                {
                    "name": folder_name,
                    "key": relative_folder_path,
                    "full_key": folder,
                }
            )

        processed_files = []
        for file in files:
            if not file.startswith(root_path):
                continue

            file_name = file[len(full_col_prefix) :]
            processed_files.append(
                {
                    "name": file_name,
                    "key": file,
                }
            )

        columns.append(
            {
                "prefix": col_prefix,
                "folders": processed_folders,
                "files": processed_files,
            }
        )

    # Identify which prefixes are currently 'active' for UI highlighting
    active_prefixes = set()
    if user_prefix:
        parts = user_prefix.rstrip("/").split("/")
        for i in range(len(parts)):
            active_prefixes.add("/".join(parts[: i + 1]) + "/")

    context = {
        "segment": "data",
        "columns": columns,
        "active_prefix": user_prefix,
        "active_prefixes": active_prefixes,
    }
    return render(request, "apps/data/files.html", context)


@login_required
@project_context_required
def download_file(request):
    """Logs access to a file and proxies the download through Django.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: The file download stream or unauthorized error.
    """
    key = request.GET.get("key")
    current_project_uuid, _ = get_user_project(request)

    if not key or not key.startswith(f"{current_project_uuid}/data/"):
        return HttpResponse("Unauthorized", status=403)

    log = logger.get_logger()
    log.access.info(f"User accessed file: {key}", file_key=key)

    # Use streaming download to avoid memory issues with large files
    return _proxy_s3_download(request, key, os.path.basename(key))


@login_required
@project_context_required
@require_POST
def delete_file(request):
    """Deletes a file or an entire folder from the project data.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: A redirect to the file list or error response.
    """
    current_project_uuid, _ = get_user_project(request)

    log = logger.get_logger()
    key = request.POST.get("key")

    # Security check: Ensure the key belongs to the current project
    if not key or not key.startswith(f"{current_project_uuid}/data/"):
        log.data.warning(f"Unauthorized delete attempt for key: {key}")
        return JsonResponse({"error": "Unauthorized"}, status=403)

    try:
        if key.endswith("/"):
            delete_s3_folder(key)
            log.data.info(f"Deleted folder: {key}")
        else:
            delete_s3_object(key)
            log.data.info(f"Deleted file: {key}")
    except Exception as e:
        log.data.error(f"Error deleting {key}: {e}")

    # Redirect back to the page the user came from, or to the file list.
    # We use get_safe_referer to prevent Open Redirect attacks.
    return redirect(get_safe_referer(request, reverse("data:list_files")))


@login_required
@project_context_required
@require_POST
def rename_file(request):
    """Renames a file or folder in S3.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: A redirect to the file list or error response.
    """
    current_project_uuid, _ = get_user_project(request)

    log = logger.get_logger()
    old_key = request.POST.get("old_key")
    new_name = request.POST.get("new_name")

    # Security check: Ensure the old_key belongs to the current project
    if not old_key or not old_key.startswith(f"{current_project_uuid}/data/"):
        log.data.warning(f"Unauthorized rename attempt for key: {old_key}")
        return JsonResponse({"error": "Unauthorized"}, status=403)

    # Sanitize new_name to prevent path traversal
    new_name = os.path.basename(new_name.rstrip("/"))

    # Determine the parent directory
    prefix = "/".join(old_key.rstrip("/").split("/")[:-1])

    # Construct the new S3 key
    if prefix:
        new_key = f"{prefix}/{new_name}"
        if old_key.endswith("/"):
            new_key += "/"
    else:
        # This case should technically not happen given our root_path structure,
        # but we handle it for robustness.
        new_key = new_name + ("/" if old_key.endswith("/") else "")

    try:
        if old_key.endswith("/"):
            rename_s3_folder(old_key, new_key)
            log.data.info(f"Renamed folder {old_key} to {new_key}")
        else:
            rename_s3_object(old_key, new_key)
            log.data.info(f"Renamed file {old_key} to {new_key}")
    except Exception as e:
        log.data.error(f"Error renaming {old_key}: {e}")

    return redirect(get_safe_referer(request, reverse("data:list_files")))


@login_required
def list_all_folders(request):
    """Returns a recursive flat list of all folders in JSON format.

    Used for folder-selection dropdowns in the UI.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        JsonResponse: A JSON list of folder objects.
    """
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse([], safe=False)

    root_path = f"{current_project_uuid}/data/"

    def collect_folders(prefix):
        folders, _ = list_s3_folder(prefix)
        all_folders = []
        for folder in folders:
            if folder.startswith(root_path):
                all_folders.append(folder)
                all_folders.extend(collect_folders(folder))
        return all_folders

    all_folders = collect_folders(root_path)

    # Format the folder list for a select2 or similar dropdown
    folder_list = []
    for folder in all_folders:
        relative_path = folder[len(root_path) :]
        folder_list.append(
            {
                "label": relative_path if relative_path else "(root)",
                "value": relative_path,
            }
        )

    # Add the root directory to the list
    if not any(item["value"] == "" for item in folder_list):
        folder_list.insert(0, {"label": "(root)", "value": ""})

    return JsonResponse(folder_list, safe=False)


# --- Data Validation Views ---


@login_required
@require_POST
def start_validation(request):
    """Triggers the background Celery task for data validation.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        JsonResponse: Result status and task identifiers.
    """
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({"error": "No project selected"}, status=400)

    try:
        project = Project.objects.get(identifier=current_project_uuid)

        if not project.data_validation_script:
            return JsonResponse(
                {"error": "No validation script found"}, status=400
            )

        # Stop any existing runs that are still pending or running
        active_runs = ValidationRun.objects.filter(
            project=project, status__in=["pending", "running"]
        )
        for run in active_runs:
            if run.celery_task_id:
                current_app.control.revoke(run.celery_task_id, terminate=True)
            run.status = "cancelled"
            run.completed_at = timezone.now()
            run.save()

        # Create a new run record
        validation_run = ValidationRun.objects.create(
            project=project, user=request.user
        )

        # Trigger the Celery task
        task = run_validation_task.delay(str(validation_run.id))
        validation_run.celery_task_id = task.id
        validation_run.save()

        return JsonResponse(
            {
                "success": True,
                "validation_run_id": str(validation_run.id),
                "task_id": task.id,
            }
        )

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@require_POST
def stop_validation(request):
    """Cancels the currently running validation task.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        JsonResponse: Result status or error.
    """
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({"error": "No project selected"}, status=400)

    project = get_object_or_404(Project, identifier=current_project_uuid)
    log = logger.get_logger(user=request.user, project=project)

    run = ValidationRun.objects.filter(
        project=project, status__in=["pending", "running"]
    ).first()

    if run:
        if run.celery_task_id:
            current_app.control.revoke(run.celery_task_id, terminate=True)
            log.data.info(
                f"Revoked Celery task {run.celery_task_id} for validation run {run.id}"
            )
        run.status = "cancelled"
        run.completed_at = timezone.now()
        run.save()
        log.data.warning(f"Validation run {run.id} cancelled by user.")
        return JsonResponse({"success": True})

    log.data.debug(
        "Stop validation requested but no running validation found."
    )
    return JsonResponse({"error": "No running validation found"}, status=404)


@login_required
def validation_status(request):
    """Returns the status and results of the latest validation run.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        JsonResponse: A JSON summary of the validation run.
    """
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({"error": "No project selected"}, status=400)

    project = get_object_or_404(Project, identifier=current_project_uuid)
    latest_run = ValidationRun.objects.filter(project=project).first()

    if not latest_run:
        return JsonResponse(
            {
                "status": "none",
                "checks": [],
                "has_script": bool(project.data_validation_script),
                "project_id": project.id,
            }
        )

    checks = list(
        ValidationCheck.objects.filter(validation_run=latest_run).values(
            "name", "status", "message", "details"
        )
    )

    return JsonResponse(
        {
            "status": latest_run.status,
            "success": latest_run.success,
            "output": latest_run.output,
            "error_message": latest_run.error_message,
            "checks": checks,
            "started_at": latest_run.started_at,
            "completed_at": latest_run.completed_at,
        }
    )


# --- Data Visualization Views ---


@login_required
@require_POST
def start_visualization(request):
    """Triggers the background Celery task for data visualization.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        JsonResponse: Result status and task identifiers.
    """
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({"error": "No project selected"}, status=400)

    try:
        project = Project.objects.get(identifier=current_project_uuid)

        if not project.data_visualization_script:
            return JsonResponse(
                {"error": "No visualization script found"}, status=400
            )

        # Stop existing visualization runs
        active_runs = VisualizationRun.objects.filter(
            project=project, status__in=["pending", "running"]
        )
        for run in active_runs:
            if run.celery_task_id:
                current_app.control.revoke(run.celery_task_id, terminate=True)
            run.status = "cancelled"
            run.completed_at = timezone.now()
            run.save()

        viz_run = VisualizationRun.objects.create(
            project=project, user=request.user
        )

        task = run_visualization_task.delay(str(viz_run.id))
        viz_run.celery_task_id = task.id
        viz_run.save()

        return JsonResponse(
            {
                "success": True,
                "visualization_run_id": str(viz_run.id),
                "task_id": task.id,
            }
        )

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@require_POST
def stop_visualization(request):
    """Cancels the currently running visualization task.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        JsonResponse: Result status or error.
    """
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({"error": "No project selected"}, status=400)

    project = get_object_or_404(Project, identifier=current_project_uuid)
    log = logger.get_logger(user=request.user, project=project)

    run = VisualizationRun.objects.filter(
        project=project, status__in=["pending", "running"]
    ).first()

    if run:
        if run.celery_task_id:
            current_app.control.revoke(run.celery_task_id, terminate=True)
            log.data.info(
                f"Revoked Celery task {run.celery_task_id} for visualization run {run.id}"
            )
        run.status = "cancelled"
        run.completed_at = timezone.now()
        run.save()
        log.data.warning(f"Visualization run {run.id} cancelled by user.")
        return JsonResponse({"success": True})

    log.data.debug(
        "Stop visualization requested but no running visualization found."
    )
    return JsonResponse(
        {"error": "No running visualization found"}, status=404
    )


@login_required
def visualization_status(request):
    """Returns the status and generated plots of the latest visualization run.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        JsonResponse: A JSON summary of the visualization run and plot URLs.
    """
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({"error": "No project selected"}, status=400)

    project = get_object_or_404(Project, identifier=current_project_uuid)
    latest_run = VisualizationRun.objects.filter(project=project).first()

    if not latest_run:
        return JsonResponse(
            {
                "status": "none",
                "plots": [],
                "has_script": bool(project.data_visualization_script),
                "project_id": project.id,
            }
        )

    plots_qs = VisualizationPlot.objects.filter(visualization_run=latest_run)
    plots = []
    for p in plots_qs:
        plots.append(
            {
                "title": p.title,
                "plot_number": p.plot_number,
                "image_url": (
                    reverse(
                        "data:get_visualization_plot", args=[p.id, "image"]
                    )
                    if p.image_data
                    else None
                ),
                "svg_url": (
                    reverse("data:get_visualization_plot", args=[p.id, "svg"])
                    if p.svg_data
                    else None
                ),
            }
        )

    return JsonResponse(
        {
            "status": latest_run.status,
            "success": latest_run.success,
            "output": latest_run.output,
            "error_message": latest_run.error_message,
            "plots": plots,
            "started_at": latest_run.started_at,
            "completed_at": latest_run.completed_at,
        }
    )


def _proxy_s3_download(request, key, filename):
    """Helper to proxy a file download from S3 through Django.

    Args:
        request (HttpRequest): The incoming HTTP request.
        key (str): The S3 object key.
        filename (str): The name to use for the download.

    Returns:
        StreamingHttpResponse: The proxied file stream.
    """
    s3 = get_s3_client()
    bucket = settings.AWS_STORAGE_BUCKET_NAME

    try:
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

                        if (
                            target_end_exclusive is not None
                            and bytes_sent >= target_end_exclusive
                        ):
                            break

                    if (
                        target_end_exclusive is None
                        or bytes_sent >= target_end_exclusive
                    ):
                        break

                    attempts += 1
                    if attempts >= max_attempts:
                        break
                except Exception:
                    attempts += 1
                    if attempts >= max_attempts:
                        break
                finally:
                    if had_progress:
                        attempts = 0

        # Force application/octet-stream to prevent Nginx from gzipping/transcoding the
        # content in-flight. When combined with streaming responses, Chrome can surface
        # a "Check internet connection" / "Network error" if a proxy modifies bytes.
        response = StreamingHttpResponse(
            stream_content(),
            content_type="application/octet-stream",
            status=status_code,
        )

        response["Accept-Ranges"] = "bytes"

        # Some clients (notably Chrome) are sensitive to mismatches between headers and
        # bytes received. If S3 provides a ContentLength, pass it through.
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

        # Disable Nginx buffering for this stream.
        response["X-Accel-Buffering"] = "no"
        return response
    except Exception as e:
        logger.get_logger().data.error(
            f"Failed to proxy S3 download for {key}: {e}"
        )
        return HttpResponse("File download failed", status=500)


def _proxy_s3_download_file(key, filename, inline=False):
    """Fully buffer the S3 object and return a plain HttpResponse.

    Args:
        key (str): The S3 object key.
        filename (str): The name to use for the download.
        inline (bool): If True, use 'inline' disposition. Defaults to False.

    Returns:
        HttpResponse: The buffered file content.
    """
    s3 = get_s3_client()
    bucket = settings.AWS_STORAGE_BUCKET_NAME

    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        data = obj["Body"].read()
        content_type = obj.get("ContentType", "application/octet-stream")
        size = len(data)

        response = HttpResponse(data, content_type=content_type)
        if inline:
            response["Content-Disposition"] = f'inline; filename="{filename}"'
        else:
            response["Content-Disposition"] = (
                f'attachment; filename="{filename}"'
            )
        response["Content-Length"] = str(size)
        response["Cache-Control"] = "no-transform"
        return response
    except Exception as e:
        logger.get_logger().data.error(
            f"Failed to download S3 file {key} via buffered HttpResponse: {e}"
        )
        return HttpResponse("File download failed", status=500)


@login_required
@project_membership_required
def get_visualization_plot(request, plot_id, plot_type):
    """Proxies a visualization plot image from S3 through Django.

    Args:
        request (HttpRequest): The incoming HTTP request.
        plot_id (str): The ID of the visualization plot.
        plot_type (str): The type of plot ('image' or 'svg').

    Returns:
        HttpResponse: The file content or error.
    """
    plot = get_object_or_404(VisualizationPlot, id=plot_id)

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

    response = _proxy_s3_download_file(key, filename, inline=True)
    return response
