"""Shared results services used by the UI and CLI."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from django.conf import settings
from django.db import models
from django.utils import timezone

from celery import current_app
from common.utils import get_s3_client
from logs import logger
from project.models import Project
from training.models import TrainingJob
from training.utils import extract_flare_job_uuid

from .artifacts import (
    is_global_model_artifact,
    is_model_artifact,
    parse_result_key,
)
from .models import ResultsVisualizationPlot, ResultsVisualizationRun, TrainingResult
from .tasks import run_results_visualization_task, sync_project_results


def _is_model_artifact(filename: str) -> bool:
    return is_model_artifact(filename)


def _etag_md5(obj: dict) -> str | None:
    """Return an S3 ETag when it is a plain MD5 digest."""
    etag = str(obj.get("ETag") or "").strip().strip('"')
    if len(etag) == 32 and "-" not in etag:
        return etag.lower()
    return None


def _file_md5(path: Path) -> str:
    """Return the hex MD5 digest for a local file."""
    digest = hashlib.md5()  # nosec B324 - checksum reporting, not security.
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_job_and_flare_id(project: Project, job_identifier: str):
    normalized_identifier = extract_flare_job_uuid(job_identifier) or job_identifier
    job = (
        TrainingJob.objects.filter(project=project)
        .filter(
            models.Q(identifier=job_identifier)
            | models.Q(flare_job_uuid=normalized_identifier)
            | models.Q(flare_job_id__icontains=job_identifier)
        )
        .order_by("-created_at")
        .first()
    )
    flare_id = normalized_identifier
    if job:
        flare_id = (
            job.flare_job_uuid
            or extract_flare_job_uuid(job.flare_job_id)
            or normalized_identifier
        )
    return job, flare_id


def sync_results(project: Project, *, asynchronous: bool = False):
    """Synchronize results from S3 into the database."""
    if asynchronous:
        return sync_project_results.delay(str(project.identifier))
    return sync_project_results(str(project.identifier))


def list_results(project: Project, *, job_identifier: str | None = None) -> list[dict]:
    """List result files for a project, optionally filtered to a single job."""
    s3 = get_s3_client()
    prefix = f"{project.identifier}/results/"
    selected_flare_id = None
    if job_identifier:
        _, selected_flare_id = _resolve_job_and_flare_id(project, job_identifier)

    db_results = {
        result.file_path: result
        for result in TrainingResult.objects.filter(job__project=project)
        .select_related("job")
        .order_by("-created_at")
    }
    result_rows: list[dict] = []

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix
    ):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parsed = parse_result_key(key, str(project.identifier))
            if not parsed:
                continue
            if not is_model_artifact(parsed.filename):
                continue
            if selected_flare_id and parsed.flare_job_id != selected_flare_id:
                continue
            db_result = db_results.get(key)
            result_rows.append(
                {
                    "identifier": str(db_result.identifier) if db_result else None,
                    "job_identifier": str(db_result.job.identifier)
                    if db_result
                    else None,
                    "flare_job_id": parsed.flare_job_id,
                    "file_path": key,
                    "relative_path": parsed.relative_path,
                    "file_size": obj.get("Size", 0),
                    "md5": _etag_md5(obj),
                    "client_name": parsed.participant,
                    "filename": parsed.filename,
                    "is_global_model": is_global_model_artifact(parsed.filename),
                    "is_legacy_layout": parsed.is_legacy_layout,
                    "created_at": (
                        db_result.created_at.isoformat()
                        if db_result
                        else obj.get("LastModified").isoformat()
                        if obj.get("LastModified")
                        else None
                    ),
                }
            )

    result_rows.sort(
        key=lambda row: row.get("created_at") or "",
        reverse=True,
    )
    return result_rows


def download_results_to_directory(
    project: Project, *, output_dir: str, job_identifier: str | None = None
) -> dict:
    """Download project result files into a local directory."""
    s3 = get_s3_client()
    bucket = settings.AWS_STORAGE_BUCKET_NAME
    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    selected_flare_id = None
    if job_identifier:
        _, selected_flare_id = _resolve_job_and_flare_id(project, job_identifier)

    prefix = f"{project.identifier}/results/"
    downloaded_files: list[str] = []
    checksums: dict[str, str] = {}
    global_model_md5s: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            parsed = parse_result_key(key, str(project.identifier))
            if not parsed:
                continue
            if not is_model_artifact(parsed.filename):
                continue
            if selected_flare_id and parsed.flare_job_id != selected_flare_id:
                continue
            target = output_path / parsed.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(target))
            downloaded_files.append(str(target))
            checksum = _file_md5(target)
            checksums[str(target)] = checksum
            if is_global_model_artifact(parsed.filename):
                global_model_md5s.append(checksum)

    if not downloaded_files:
        raise LookupError("No result files were found for the requested scope.")

    return {
        "project_identifier": str(project.identifier),
        "job_identifier": job_identifier,
        "output_dir": str(output_path),
        "downloaded_files": downloaded_files,
        "checksums": checksums,
        "global_model_md5s": sorted(set(global_model_md5s)),
    }


def build_results_dashboard_context(
    project: Project,
    *,
    selected_job_id: str | None = None,
    default_to_latest: bool = True,
    asynchronous_sync: bool = True,
) -> dict:
    """Build the Hub results dashboard context from the shared S3 layout rules."""
    log = logger.get_logger(project=project)
    if asynchronous_sync:
        try:
            sync_results(project, asynchronous=True)
            log.results.debug(
                f"Triggered results sync for project {project.identifier}"
            )
        except Exception as exc:
            log.results.error(f"Failed to trigger results sync: {exc}")

    s3 = get_s3_client()
    prefix = f"{project.identifier}/results/"
    paginator = s3.get_paginator("list_objects_v2")

    job_ids_in_s3: set[str] = set()
    job_last_modified = {}
    s3_items: list[dict] = []

    try:
        for page in paginator.paginate(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix
        ):
            for obj in page.get("Contents", []):
                parsed = parse_result_key(obj["Key"], str(project.identifier))
                if not parsed:
                    continue
                job_ids_in_s3.add(parsed.flare_job_id)

                last_modified = obj.get("LastModified")
                if last_modified:
                    previous = job_last_modified.get(parsed.flare_job_id)
                    job_last_modified[parsed.flare_job_id] = (
                        max(previous, last_modified) if previous else last_modified
                    )

                s3_items.append(
                    {
                        "key": obj["Key"],
                        "size": obj.get("Size", 0),
                        "last_modified": last_modified,
                        "parsed": parsed,
                    }
                )
    except Exception as exc:
        log.results.error(f"Error listing S3 objects: {exc}")

    all_project_jobs = TrainingJob.objects.filter(project=project).order_by(
        "-created_at"
    )
    db_results = {
        result.file_path: result
        for result in TrainingResult.objects.filter(job__project=project)
    }

    job_options = []
    for s3_job_id in list(job_ids_in_s3):
        normalized_s3_job_id = extract_flare_job_uuid(s3_job_id) or s3_job_id
        db_job = all_project_jobs.filter(
            models.Q(flare_job_uuid=normalized_s3_job_id)
            | models.Q(flare_job_id__icontains=normalized_s3_job_id)
        ).first()
        last_modified = job_last_modified.get(s3_job_id)
        if db_job:
            label = (
                f"{db_job.created_at.strftime('%Y-%m-%d %H:%M:%S')} "
                f"({s3_job_id[:8]})"
            )
            sort_time = db_job.created_at
        else:
            label = (
                last_modified.strftime("%Y-%m-%d %H:%M:%S")
                if last_modified
                else s3_job_id
            )
            sort_time = last_modified if last_modified else timezone.now()
        job_options.append(
            {"value": s3_job_id, "label": label, "last_modified": sort_time}
        )

    job_options.sort(key=lambda row: row["last_modified"], reverse=True)

    effective_selected_job_id = selected_job_id
    if default_to_latest and effective_selected_job_id is None and job_options:
        effective_selected_job_id = job_options[0]["value"]

    if effective_selected_job_id:
        s3_items = [
            item
            for item in s3_items
            if item["parsed"].flare_job_id == effective_selected_job_id
        ]

    prepared_results = []
    for item in s3_items:
        key = item["key"]
        parsed = item["parsed"]
        db_record = db_results.get(key)
        prepared_results.append(
            {
                "id": db_record.id if db_record else None,
                "file_path": key,
                "file_size": item["size"],
                "file_type": os.path.splitext(key)[1].lstrip(".").lower()
                or "unknown",
                "cleaned_filename": os.path.basename(key),
                "uploaded_at": (
                    db_record.created_at if db_record else item.get("last_modified")
                ),
                "client_name": parsed.participant,
            }
        )

    prepared_results.sort(
        key=lambda row: row["uploaded_at"] or timezone.now(), reverse=True
    )

    selected_job_details = None
    if effective_selected_job_id:
        normalized_selected_job_id = (
            extract_flare_job_uuid(effective_selected_job_id)
            or effective_selected_job_id
        )
        selected_job_details = all_project_jobs.filter(
            models.Q(flare_job_uuid=normalized_selected_job_id)
            | models.Q(flare_job_id__icontains=normalized_selected_job_id)
        ).first()

    if (
        default_to_latest
        and not selected_job_details
        and all_project_jobs.exists()
    ):
        selected_job_details = all_project_jobs.first()
        if selected_job_details:
            effective_selected_job_id = (
                selected_job_details.flare_job_uuid
                or extract_flare_job_uuid(selected_job_details.flare_job_id)
                or selected_job_details.flare_job_id
            )

    return {
        "segment": "results",
        "project": project,
        "results": prepared_results,
        "project_identifier": str(project.identifier),
        "job_options": job_options,
        "selected_job_id": effective_selected_job_id,
        "selected_job_details": selected_job_details,
        "has_jobs": bool(job_options),
    }


def start_results_visualization(
    *, project: Project, user, job_identifier: str
) -> ResultsVisualizationRun:
    """Start a results visualization task."""
    log = logger.get_logger(user=user, project=project)
    job, flare_id = _resolve_job_and_flare_id(project, job_identifier)

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
        raise ValueError(
            f"No visualization scripts found at {script_prefix}. "
            "Upload one on the project page first."
        )

    running_query = ResultsVisualizationRun.objects.filter(
        project=project,
        status__in=["pending", "running"],
    )
    if job:
        running_query = running_query.filter(
            models.Q(job=job)
            | models.Q(flare_job_id=flare_id)
            | models.Q(flare_job_id__icontains=flare_id)
        )
    else:
        running_query = running_query.filter(
            models.Q(flare_job_id=flare_id)
            | models.Q(flare_job_id__icontains=flare_id)
        )
    for visualization in running_query:
        if visualization.celery_task_id:
            current_app.control.revoke(
                visualization.celery_task_id, terminate=True
            )
        visualization.status = "cancelled"
        visualization.completed_at = timezone.now()
        visualization.save(update_fields=["status", "completed_at"])
        log.results.info(
            f"Cancelled previous results visualization run {visualization.id}"
        )

    visualization_run = ResultsVisualizationRun.objects.create(
        project=project,
        job=job,
        flare_job_id=flare_id,
        user=user,
    )
    task = run_results_visualization_task.delay(
        str(visualization_run.id), flare_id
    )
    visualization_run.celery_task_id = task.id
    visualization_run.save(update_fields=["celery_task_id"])
    return visualization_run


def stop_results_visualization(*, user, run_id: str) -> ResultsVisualizationRun:
    """Cancel a running results visualization task."""
    visualization_run = ResultsVisualizationRun.objects.filter(
        id=run_id, user=user
    ).first()
    if not visualization_run:
        raise LookupError("Visualization run not found.")
    if visualization_run.status not in ["pending", "running"]:
        raise LookupError("No running visualization found.")

    if visualization_run.celery_task_id:
        current_app.control.revoke(
            visualization_run.celery_task_id, terminate=True
        )
    visualization_run.status = "cancelled"
    visualization_run.completed_at = timezone.now()
    visualization_run.save(update_fields=["status", "completed_at"])
    return visualization_run


def get_results_visualization_run(
    project: Project, *, job_identifier: str
) -> ResultsVisualizationRun | None:
    """Return the latest results visualization run for a job."""
    job, flare_id = _resolve_job_and_flare_id(project, job_identifier)
    query = ResultsVisualizationRun.objects.filter(project=project)
    if job:
        query = query.filter(
            models.Q(job=job)
            | models.Q(flare_job_id=flare_id)
            | models.Q(flare_job_id__icontains=flare_id)
        )
    else:
        query = query.filter(
            models.Q(flare_job_id=flare_id)
            | models.Q(flare_job_id__icontains=flare_id)
        )
    return query.first()


def resolve_results_visualization_plot(
    project: Project, plot_id: str
) -> ResultsVisualizationPlot | None:
    """Resolve a results visualization plot.

    UUID identifiers are preferred, while numeric ids are accepted for legacy
    plot URLs. Both lookups stay scoped to the active project.
    """
    base = ResultsVisualizationPlot.objects.filter(
        visualization_run__project=project
    )
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
    try:
        legacy_id = int(plot_id)
    except ValueError:
        return None
    return base.filter(id=legacy_id).first()


def serialize_results_visualization_run(
    run: ResultsVisualizationRun | None,
) -> dict:
    """Serialize a results visualization run."""
    if not run:
        return {"status": "none", "plots": []}

    plots = [
        {
            "identifier": str(plot.identifier),
            "title": plot.title,
            "plot_number": plot.plot_number,
            "image_key": plot.image_data.name if plot.image_data else None,
            "svg_key": plot.svg_data.name if plot.svg_data else None,
        }
        for plot in ResultsVisualizationPlot.objects.filter(
            visualization_run=run
        ).order_by("plot_number")
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
        "job_identifier": str(run.job.identifier) if run.job else None,
        "flare_job_id": run.flare_job_id,
    }
