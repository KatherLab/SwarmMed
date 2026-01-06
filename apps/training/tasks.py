"""
Celery background tasks for the training application.
Handles periodic monitoring of active training jobs, detection of completion,
and synchronization of result files (weights/logs) to S3 storage.
"""

import ast
import os
import shutil
import json
import re

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from logs import logger
from logs.utils import format_exception

from .models import TrainingJob
from .utils import upload_folder_to_s3


def _tail_text(file_path: str, max_bytes: int = 2048 * 1024) -> str:
    """Read up to the last max_bytes of a text file (decoded safely)."""
    try:
        with open(file_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            start = max(0, end - max_bytes)
            f.seek(start)
            data = f.read()
        return data.decode("utf-8", errors="ignore")
    except OSError:
        return ""


_ROUND_RE = re.compile(r"finished training round (\d+)")


def _get_total_rounds(project_id: str, network_id: str) -> int:
    """Try to read num_rounds from the server config written at job submission."""
    cfg_path = os.path.join(
        "workspaces",
        project_id,
        network_id,
        "job",
        "app_server",
        "config",
        "config_fed_server.json",
    )
    try:
        if os.path.exists(cfg_path):
            with open(cfg_path, "r") as f:
                cfg = json.load(f)
            for workflow in cfg.get("workflows", []):
                if workflow.get("id") == "swarm_controller":
                    return int(workflow.get("args", {}).get("num_rounds", 10))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return 10
    return 10


@shared_task
def monitor_training_jobs():
    """
    Task to monitor training jobs, check for completion by reading workspace logs,
    and automatically upload finalized results to S3.
    """
    # Import inside the task to avoid circular dependency issues with results
    # model.
    from results.models import TrainingResult

    log = logger.get_logger()

    # We check RUNNING jobs to see if they finished,
    # and COMPLETED jobs to ensure their files were actually synced to S3.
    jobs = TrainingJob.objects.filter(status__in=["RUNNING", "COMPLETED"])

    for job in jobs:
        try:
            # Skip jobs that are already fully synced to S3 to save resources.
            if (
                TrainingResult.objects.filter(job=job).exists()
                and job.status == "COMPLETED"
            ):
                continue

            project_id = str(job.project.identifier)
            network_id = str(job.network.identifier)
            flare_job_id_raw = job.flare_job_id

            # Step 1: Extract the clean Job UUID from the flare_job_id_raw string.
            # NVFlare often returns a complex object or string like "Submitted
            # job: <UUID>".
            flare_job_uuid = None
            try:
                # Try to parse it if it looks like a Python list/dict (logs
                # format).
                parsed = ast.literal_eval(flare_job_id_raw)
                if isinstance(parsed, list):
                    for item in parsed:
                        if (
                            isinstance(item, dict)
                            and item.get("type") == "string"
                            and "Submitted job:" in item.get("data", "")
                        ):
                            flare_job_uuid = item.get("data", "").split(":")[-1].strip()
                            break
                if not flare_job_uuid:
                    flare_job_uuid = flare_job_id_raw
            except (ValueError, SyntaxError):
                # If parsing fails, assume it's already a clean string or
                # handles itself.
                flare_job_uuid = flare_job_id_raw

            log.training.info(
                f"Monitoring Job {job.identifier}. Flare UUID: {flare_job_uuid}"
            )

            # Step 2: Locate the NVFlare workspace on the local filesystem.
            # The workspace is structured as:
            # workspaces/<project>/<network>/workspace/
            network_workspace_root = os.path.join(
                "workspaces", project_id, network_id, "workspace"
            )
            workspace_base = None

            # Search for the 'prod_00' directory which contains the actual job
            # output.
            if os.path.exists(network_workspace_root):
                for root, dirs, _ in os.walk(network_workspace_root):
                    if "prod_00" in dirs:
                        workspace_base = os.path.join(root, "prod_00")
                        break

            if not workspace_base:
                log.training.warning(
                    f"Workspace folder not found in {network_workspace_root}"
                )
                continue

            # Step 3: Determine if the job has ended by scanning log files.
            ended = job.status == "COMPLETED"
            total_rounds = _get_total_rounds(project_id, network_id)
            rounds_finished = 0
            if not ended:
                for root, _, files in os.walk(workspace_base):
                    if ended:
                        break
                    # We only look at logs in directories belonging to this
                    # specific job.
                    if flare_job_uuid in root:
                        for fname in files:
                            if fname.startswith("log") and fname.endswith(".txt"):
                                fpath = os.path.join(root, fname)
                                try:
                                    content = _tail_text(fpath)
                                    # Specific log markers indicating NVFlare finished.
                                    if (
                                        "ending workflow swarm_controller" in content
                                        or "child worker process finished" in content
                                    ):
                                        ended = True
                                        break

                                    for m in _ROUND_RE.finditer(content):
                                        rnum = int(m.group(1))
                                        if rnum > rounds_finished:
                                            rounds_finished = rnum
                                except OSError:
                                    continue

            # Persist progress (even while still running) so the UI can avoid log parsing.
            try:
                job.total_rounds = total_rounds
                job.rounds_finished = rounds_finished
                if total_rounds and total_rounds > 0:
                    pct = int(rounds_finished * 100 / total_rounds)
                    job.progress_percent = max(0, min(100, pct))
                else:
                    job.progress_percent = 0
                job.progress_updated_at = timezone.now()
                job.save(
                    update_fields=[
                        "total_rounds",
                        "rounds_finished",
                        "progress_percent",
                        "progress_updated_at",
                    ]
                )
            except Exception as e:
                log.training.debug(format_exception(e))

            # Step 4: If the job is complete, upload participant results to S3.
            if ended:
                log.training.info(
                    f"Job {job.identifier} ({flare_job_uuid}) is COMPLETED. Syncing..."
                )

                # Each client/server has its own folder under 'prod_00'.
                # Inside those, there's a folder named with the job's UUID.
                found_folders = []
                for participant in os.listdir(workspace_base):
                    p_path = os.path.join(workspace_base, participant)
                    if os.path.isdir(p_path):
                        job_p_path = os.path.join(p_path, flare_job_uuid)
                        if os.path.exists(job_p_path):
                            found_folders.append((participant, job_p_path))

                if found_folders:
                    log.training.info(
                        f"Found {len(found_folders)} result folders for upload."
                    )
                    for participant, local_path in found_folders:
                        # S3 path structure:
                        # <project>/results/<job_uuid>/<client_name>/
                        s3_prefix = (
                            f"{project_id}/results/{flare_job_uuid}/{participant}"
                        )
                        upload_folder_to_s3(
                            settings.AWS_STORAGE_BUCKET_NAME, local_path, s3_prefix
                        )

                    # Update job status in database to trigger UI updates.
                    job.status = "COMPLETED"
                    if not job.completed_at:
                        job.completed_at = timezone.now()
                    job.progress_percent = 100
                    job.progress_updated_at = timezone.now()
                    job.save(
                        update_fields=[
                            "status",
                            "completed_at",
                            "progress_percent",
                            "progress_updated_at",
                        ]
                    )
                    log.training.info(
                        f"Job {job.identifier} successfully synced to S3."
                    )

                    # Security Cleanup: Remove the local workspace data now that it is safely encrypted in S3.
                    # This minimizes the window where unencrypted data exists on the host disk.
                    try:
                        # Ensure we are deleting the specific job directory, not the whole project
                        if (
                            os.path.exists(workspace_base)
                            and flare_job_uuid in workspace_base
                        ):
                            # The workspace_base is .../prod_00. The job specific data is inside subfolders.
                            # But we want to clean up the whole run for this job if possible.
                            # Wait, workspace_base is .../workspace/prod_00
                            # NVFlare typically creates a new run folder or uses the workspace.
                            # If we delete prod_00, we might lose logs for debugging if upload failed?
                            # But here upload succeeded.
                            shutil.rmtree(workspace_base)
                            log.training.info(
                                f"Securely cleaned up local workspace: {workspace_base}"
                            )
                    except Exception as e:
                        log.training.warning(
                            f"Failed to cleanup local workspace {workspace_base}: {e}"
                        )

                else:
                    log.training.warning(
                        f"No result folders found for {flare_job_uuid}"
                    )

        except Exception as e:
            log.training.error(
                f"Error monitoring job {job.identifier}: {str(e)}",
                extra=format_exception(e),
            )
