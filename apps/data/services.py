"""Shared data services used by the UI and CLI."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.utils import timezone

from celery import current_app
from common.utils import get_s3_client
from logs import logger
from project.models import Project

from .models import ValidationCheck, ValidationRun, VisualizationPlot, VisualizationRun
from .tasks import run_validation_task, run_visualization_task
from .utils import (
    delete_s3_folder,
    delete_s3_object,
    invalidate_s3_caches,
    list_s3_folder,
    rename_s3_folder,
    rename_s3_object,
)


def _normalize_relative_path(path: str, *, allow_empty: bool = True) -> str:
    normalized = os.path.normpath(path or "").lstrip(os.path.sep + (os.path.altsep or ""))
    if normalized in {"", "."}:
        return "" if allow_empty else "."
    if normalized.startswith("..") or os.path.isabs(path or ""):
        raise ValueError(f"Invalid relative path '{path}'.")
    return normalized.replace("\\", "/")


def _project_data_root(project: Project) -> str:
    return f"{project.identifier}/data/"


def _project_data_key(project: Project, relative_path: str) -> str:
    clean_path = _normalize_relative_path(relative_path)
    if not clean_path:
        return _project_data_root(project)
    return f"{_project_data_root(project)}{clean_path}"


def _project_data_prefix(project: Project, relative_path: str) -> str:
    clean_path = _normalize_relative_path(relative_path)
    if not clean_path:
        return _project_data_root(project)
    return f"{_project_data_root(project)}{clean_path.rstrip('/')}/"


def _prefix_has_objects(prefix: str) -> bool:
    s3 = get_s3_client()
    response = s3.list_objects_v2(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Prefix=prefix,
        MaxKeys=1,
    )
    return bool(response.get("Contents"))


def _is_directory(project: Project, relative_path: str) -> bool:
    return _prefix_has_objects(_project_data_prefix(project, relative_path))


def list_project_data(project: Project, prefix: str = "") -> dict:
    """Return immediate folders and files for a data prefix."""
    clean_prefix = _normalize_relative_path(prefix)
    full_prefix = (
        _project_data_root(project)
        if not clean_prefix
        else f"{_project_data_root(project)}{clean_prefix.rstrip('/')}/"
    )
    folders, files = list_s3_folder(full_prefix)
    return {
        "prefix": clean_prefix,
        "folders": [
            {
                "name": folder[len(full_prefix):].rstrip("/"),
                "path": folder[len(_project_data_root(project)) :].rstrip("/"),
            }
            for folder in folders
            if folder.startswith(_project_data_root(project))
        ],
        "files": [
            {
                "name": file[len(full_prefix) :],
                "path": file[len(_project_data_root(project)) :],
            }
            for file in files
            if file.startswith(_project_data_root(project))
        ],
    }


def upload_data_entries(
    project: Project,
    *,
    entries: list[tuple[str, object]],
    destination_folder: str = "",
) -> dict:
    """Upload multiple file entries into project data storage."""
    destination = _normalize_relative_path(destination_folder)
    prefix = _project_data_root(project)
    if destination:
        prefix = f"{prefix}{destination.rstrip('/')}/"

    saved: list[str] = []
    skipped: list[dict] = []

    for relative_path, file_object in entries:
        try:
            clean_rel_path = _normalize_relative_path(relative_path, allow_empty=False)
        except ValueError as exc:
            skipped.append({"path": relative_path, "reason": str(exc)})
            continue

        _, extension = os.path.splitext(clean_rel_path)
        if extension.lower() not in settings.ALLOWED_EXTENSIONS:
            skipped.append(
                {
                    "path": relative_path,
                    "reason": f"Disallowed file extension '{extension}'.",
                }
            )
            continue

        storage_key = f"{prefix}{clean_rel_path}"
        default_storage.save(storage_key, file_object)
        invalidate_s3_caches(storage_key)
        saved.append(storage_key[len(_project_data_root(project)) :])

    log = logger.get_logger(project=project)
    if saved:
        log.data.info(
            f"Uploaded {len(saved)} data file(s) into {destination or '(root)'}."
        )
    for warning in skipped:
        log.data.warning(
            f"Skipped uploading '{warning['path']}': {warning['reason']}"
        )

    return {"saved": saved, "skipped": skipped}


def upload_local_sources(
    project: Project, *, sources: list[str], destination_folder: str = ""
) -> dict:
    """Upload local files or directories into project data storage."""
    entries: list[tuple[str, object]] = []
    handles: list[object] = []

    try:
        for source in sources:
            source_path = Path(source).expanduser().resolve()
            if not source_path.exists():
                raise ValueError(f"Source path '{source}' does not exist.")

            if source_path.is_file():
                handle = source_path.open("rb")
                handles.append(handle)
                entries.append((source_path.name, File(handle)))
                continue

            for child in sorted(source_path.rglob("*")):
                if not child.is_file():
                    continue
                handle = child.open("rb")
                handles.append(handle)
                relative = f"{source_path.name}/{child.relative_to(source_path).as_posix()}"
                entries.append((relative, File(handle)))

        return upload_data_entries(
            project, entries=entries, destination_folder=destination_folder
        )
    finally:
        for handle in handles:
            handle.close()


def upload_request_files(
    project: Project,
    *,
    files,
    directories: dict[str, str],
    destination_folder: str = "",
) -> dict:
    """Upload request files from the web UI into project data storage."""
    entries: list[tuple[str, object]] = []
    for idx, file_obj in enumerate(files):
        key = f"{file_obj.name}_{idx}"
        rel_path = directories.get(key, file_obj.name)
        entries.append((rel_path, file_obj))
    return upload_data_entries(
        project, entries=entries, destination_folder=destination_folder
    )


def delete_project_data(project: Project, relative_path: str) -> dict:
    """Delete a project data file or folder."""
    if _is_directory(project, relative_path):
        target = _project_data_prefix(project, relative_path)
        delete_s3_folder(target)
        return {"deleted": relative_path.rstrip("/"), "type": "folder"}

    target = _project_data_key(project, relative_path)
    delete_s3_object(target)
    return {"deleted": relative_path, "type": "file"}


def move_project_data(project: Project, source: str, destination: str) -> dict:
    """Move or rename a project data file or folder."""
    if _is_directory(project, source):
        old_prefix = _project_data_prefix(project, source)
        new_prefix = _project_data_prefix(project, destination)
        rename_s3_folder(old_prefix, new_prefix)
        return {
            "source": source.rstrip("/"),
            "destination": destination.rstrip("/"),
            "type": "folder",
        }

    old_key = _project_data_key(project, source)
    new_key = _project_data_key(project, destination)
    rename_s3_object(old_key, new_key)
    return {"source": source, "destination": destination, "type": "file"}


def download_project_data(
    project: Project, *, relative_path: str, output_path: str
) -> dict:
    """Download a project data file or folder to local disk."""
    s3 = get_s3_client()
    bucket = settings.AWS_STORAGE_BUCKET_NAME
    output = Path(output_path).expanduser().resolve()

    if _is_directory(project, relative_path):
        prefix = _project_data_prefix(project, relative_path)
        output.mkdir(parents=True, exist_ok=True)
        paginator = s3.get_paginator("list_objects_v2")
        downloaded: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                rel_name = key[len(prefix) :]
                target = output / rel_name
                target.parent.mkdir(parents=True, exist_ok=True)
                s3.download_file(bucket, key, str(target))
                downloaded.append(str(target))
        return {
            "relative_path": relative_path.rstrip("/"),
            "output_path": str(output),
            "downloaded_files": downloaded,
            "type": "folder",
        }

    key = _project_data_key(project, relative_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, key, str(output))
    return {
        "relative_path": relative_path,
        "output_path": str(output),
        "downloaded_files": [str(output)],
        "type": "file",
    }


def start_validation(project: Project, user) -> ValidationRun:
    """Create and enqueue a validation run."""
    if not project.data_validation_script:
        raise ValueError("No validation script found for the project.")

    active_runs = ValidationRun.objects.filter(
        project=project, status__in=["pending", "running"]
    )
    for run in active_runs:
        if run.celery_task_id:
            current_app.control.revoke(run.celery_task_id, terminate=True)
        run.status = "cancelled"
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "completed_at"])

    validation_run = ValidationRun.objects.create(project=project, user=user)
    task = run_validation_task.delay(str(validation_run.id))
    validation_run.celery_task_id = task.id
    validation_run.save(update_fields=["celery_task_id"])
    return validation_run


def stop_validation(project: Project) -> ValidationRun:
    """Cancel the active validation run."""
    run = ValidationRun.objects.filter(
        project=project, status__in=["pending", "running"]
    ).first()
    if not run:
        raise LookupError("No running validation found.")
    if run.celery_task_id:
        current_app.control.revoke(run.celery_task_id, terminate=True)
    run.status = "cancelled"
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "completed_at"])
    return run


def get_latest_validation_run(project: Project) -> ValidationRun | None:
    """Return the latest validation run for a project."""
    return ValidationRun.objects.filter(project=project).first()


def serialize_validation_run(run: ValidationRun | None, project: Project | None = None) -> dict:
    """Serialize a validation run for CLI or JSON responses."""
    if not run:
        return {
            "status": "none",
            "has_script": bool(project and project.data_validation_script),
        }
    checks = list(
        ValidationCheck.objects.filter(validation_run=run).values(
            "name", "status", "message", "details"
        )
    )
    return {
        "identifier": str(run.identifier),
        "status": run.status,
        "success": run.success,
        "output": run.output,
        "error_message": run.error_message,
        "checks": checks,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat()
        if run.completed_at
        else None,
    }


def start_visualization(project: Project, user) -> VisualizationRun:
    """Create and enqueue a data visualization run."""
    if not project.data_visualization_script:
        raise ValueError("No visualization script found for the project.")

    active_runs = VisualizationRun.objects.filter(
        project=project, status__in=["pending", "running"]
    )
    for run in active_runs:
        if run.celery_task_id:
            current_app.control.revoke(run.celery_task_id, terminate=True)
        run.status = "cancelled"
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "completed_at"])

    visualization_run = VisualizationRun.objects.create(
        project=project, user=user
    )
    task = run_visualization_task.delay(str(visualization_run.id))
    visualization_run.celery_task_id = task.id
    visualization_run.save(update_fields=["celery_task_id"])
    return visualization_run


def stop_visualization(project: Project) -> VisualizationRun:
    """Cancel the active data visualization run."""
    run = VisualizationRun.objects.filter(
        project=project, status__in=["pending", "running"]
    ).first()
    if not run:
        raise LookupError("No running visualization found.")
    if run.celery_task_id:
        current_app.control.revoke(run.celery_task_id, terminate=True)
    run.status = "cancelled"
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "completed_at"])
    return run


def get_latest_visualization_run(project: Project) -> VisualizationRun | None:
    """Return the latest data visualization run for a project."""
    return VisualizationRun.objects.filter(project=project).first()


def resolve_visualization_plot(
    project: Project, plot_id: str
) -> VisualizationPlot | None:
    """Resolve a visualization plot by UUID identifier or numeric id.

    Returns ``None`` when no plot matches; callers (Hub view or CLI) decide
    whether to 404 or surface a structured error.
    """
    base = VisualizationPlot.objects.filter(visualization_run__project=project)
    plot_id = (plot_id or "").strip()
    if not plot_id:
        return None
    try:
        plot_uuid = uuid.UUID(plot_id)
    except ValueError:
        plot_uuid = None
    if plot_uuid:
        plot = base.filter(identifier=plot_uuid).first()
        if plot is not None:
            return plot
    # Legacy fallback: older serialized payloads referenced the numeric primary
    # key. Accept it but only if it parses as an integer.
    try:
        legacy_id = int(plot_id)
    except ValueError:
        return None
    return base.filter(id=legacy_id).first()


def serialize_visualization_run(
    run: VisualizationRun | None, project: Project | None = None
) -> dict:
    """Serialize a data visualization run for CLI or JSON responses."""
    if not run:
        return {
            "status": "none",
            "has_script": bool(project and project.data_visualization_script),
            "plots": [],
        }

    plots = [
        {
            "identifier": str(plot.identifier),
            "title": plot.title,
            "plot_number": plot.plot_number,
            "image_key": plot.image_data.name if plot.image_data else None,
            "svg_key": plot.svg_data.name if plot.svg_data else None,
        }
        for plot in VisualizationPlot.objects.filter(visualization_run=run)
    ]
    return {
        "identifier": str(run.identifier),
        "status": run.status,
        "success": run.success,
        "output": run.output,
        "error_message": run.error_message,
        "plots": plots,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat()
        if run.completed_at
        else None,
    }
