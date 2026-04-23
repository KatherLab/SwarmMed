"""Shared results services used by the UI and CLI."""

from __future__ import annotations

import ast
import os
from pathlib import Path

from celery import current_app
from django.conf import settings
from django.db import models
from django.utils import timezone

from common.utils import get_s3_client
from logs import logger
from project.models import Project
from training.models import TrainingJob

from .models import ResultsVisualizationPlot, ResultsVisualizationRun, TrainingResult
from .tasks import run_results_visualization_task, sync_project_results


def _resolve_job_and_flare_id(project: Project, job_identifier: str):
    job = TrainingJob.objects.filter(
        models.Q(identifier=job_identifier)
        | models.Q(flare_job_id__icontains=job_identifier),
        project=project,
    ).first()
    flare_id = job_identifier
    if job:
        flare_id = job.flare_job_id
        try:
            parsed = ast.literal_eval(flare_id)
            if isinstance(parsed, list):
                for item in parsed:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "string"
                        and "Submitted job:" in item.get("data", "")
                    ):
                        flare_id = item.get("data", "").split(":")[-1].strip()
                        break
        except (ValueError, SyntaxError):
            pass
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
            if key.endswith("/"):
                continue
            parts = key.split("/")
            if len(parts) < 4:
                continue
            flare_id = parts[2]
            if selected_flare_id and flare_id != selected_flare_id:
                continue
            db_result = db_results.get(key)
            result_rows.append(
                {
                    "identifier": str(db_result.identifier) if db_result else None,
                    "job_identifier": str(db_result.job.identifier)
                    if db_result
                    else None,
                    "flare_job_id": flare_id,
                    "file_path": key,
                    "relative_path": key[len(prefix) :],
                    "file_size": obj.get("Size", 0),
                    "client_name": parts[3] if len(parts) > 3 else "unknown",
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
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            parts = key.split("/")
            if len(parts) < 4:
                continue
            flare_id = parts[2]
            if selected_flare_id and flare_id != selected_flare_id:
                continue
            relative_name = key[len(prefix) :]
            target = output_path / relative_name
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(target))
            downloaded_files.append(str(target))

    if not downloaded_files:
        raise LookupError("No result files were found for the requested scope.")

    return {
        "project_identifier": str(project.identifier),
        "job_identifier": job_identifier,
        "output_dir": str(output_path),
        "downloaded_files": downloaded_files,
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
            f"No visualization scripts found at {script_prefix}. Upload one on the project page first."
        )

    running_query = ResultsVisualizationRun.objects.filter(
        project=project,
        status__in=["pending", "running"],
        flare_job_id=flare_id,
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
    _, flare_id = _resolve_job_and_flare_id(project, job_identifier)
    return ResultsVisualizationRun.objects.filter(
        project=project, flare_job_id=flare_id
    ).first()


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
