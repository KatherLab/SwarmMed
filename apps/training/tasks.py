"""Celery background tasks for the training application.

Handles periodic monitoring of active training jobs, detection of completion,
and synchronization of result files (weights/logs) to S3 storage.
"""

import contextlib
import json
import os

from django.conf import settings
from django.utils import timezone

from celery import shared_task
from logs import logger
from logs.utils import format_exception
from network.models import SwarmNetwork

from . import services as training_services
from .models import TrainingJob
from .runtime import (
    new_secure_session_with_host,
    parse_nvflare_jobs,
    resolve_admin_session_target,
)
from .utils import (
    extract_flare_job_uuid,
    extract_total_rounds_from_config_file,
    progress_from_rounds,
    summarize_training_log,
    should_update_terminal_status,
    upload_folder_to_s3,
)


def _get_total_rounds(workspace_base):
    """Helper to extract the configured number of training rounds from NVFlare config.

    Searches for config_fed_server.json anywhere in workspace_base.

    Args:
        workspace_base (str): The base directory of the NVFlare workspace.

    Returns:
        int: The total number of rounds as an integer. Defaults to 10.
    """
    # Search for config_fed_server.json anywhere in workspace_base
    for root, _dirs, files in os.walk(workspace_base):
        if "config_fed_server.json" in files:
            cfg_path = os.path.join(root, "config_fed_server.json")
            rounds = extract_total_rounds_from_config_file(cfg_path, default=10)
            if rounds != 10:
                return rounds
    return 10


def _select_result_root(target: str, app_subdir: str) -> str:
    """Prefer the participant job root when it contains result artifacts."""
    artifact_markers = {
        "audit.log",
        "fl_app.txt",
        "log.json",
        "log.txt",
        "log_error.txt",
        "log_fl.txt",
        "meta.json",
        "stats_pool_summary.json",
    }
    if os.path.isdir(os.path.join(target, "models")):
        return target
    if os.path.exists(target):
        try:
            if artifact_markers.intersection(os.listdir(target)):
                return target
        except OSError:
            pass
    if os.path.exists(app_subdir):
        return app_subdir
    return target


@shared_task
def monitor_training_jobs():
    """Periodic task to monitor training jobs and sync results.

    Discovers new jobs from the FLARE server, scrapes local Docker logs for 
    real-time progress, and automatically uploads finalized results to S3.
    """
    from network.utils import get_tailscale_ip

    from .utils import scrape_docker_progress

    local_ip = get_tailscale_ip()
    log = logger.get_logger()

    # 1. DISCOVERY: Find jobs on the FLARE server that aren't in our local database.
    active_networks = SwarmNetwork.objects.filter(
        status__in=["RUNNING", "STARTING", "PROVISIONED"]
    )

    for network in active_networks:
        admin_target = resolve_admin_session_target(network)
        if not admin_target:
            continue

        try:
            admin_name, admin_dir, server_ip = admin_target
            sess = new_secure_session_with_host(
                username=admin_name,
                startup_kit_location=admin_dir,
                host=server_ip,
                timeout=10.0,
                network_id=network.identifier,
            )

            response = sess.api.do_command("list_jobs")
            remote_jobs = parse_nvflare_jobs(response)

            for rj in remote_jobs:
                job_id = str(rj.get("job_id") or rj.get("id") or "")
                if not job_id:
                    continue

                status = str(
                    rj.get("status") or rj.get("state") or "RUNNING"
                ).upper()
                log.training.info(
                    f"Monitor: Discovered new remote job: {job_id} ({status})"
                )
                job, _created = training_services.ensure_training_job(
                    network=network,
                    flare_job_id=job_id,
                    status=(
                        status
                        if status in ["RUNNING", "COMPLETED", "FAILED", "STOPPED"]
                        else "RUNNING"
                    ),
                )
                training_services.persist_training_job_state(
                    job,
                    flare_job_id=job_id,
                    status=(
                        status
                        if status in ["RUNNING", "COMPLETED", "FAILED", "STOPPED"]
                        else "RUNNING"
                    ),
                    completed_at=timezone.now()
                    if status in {"COMPLETED", "FAILED", "STOPPED"}
                    else None,
                )

            with contextlib.suppress(Exception):
                sess.close()
        except Exception:
            continue

    # 2. LOCAL DOCKER SCRAPING: Check running containers for progress
    for network in active_networks:
        try:
            local_participants = network.participants.filter(ip=local_ip)
            participant_ids = [p.participant_id for p in local_participants]
            docker_results = scrape_docker_progress(
                participant_ids=participant_ids
            )

            for res in docker_results:
                job_id = res["job_id"]
                if not job_id:
                    recent_job = (
                        TrainingJob.objects.filter(
                            network=network, status="RUNNING"
                        )
                        .order_by("-created_at")
                        .first()
                    )
                    if recent_job:
                        job_id = recent_job.flare_job_id

                if job_id:
                    training_services.sync_job_from_docker_result(
                        network=network, result=res
                    )
        except Exception:
            continue

    # 3. MONITORING: Check RUNNING jobs (filesystem logs).
    jobs = TrainingJob.objects.filter(status__in=["RUNNING", "COMPLETED"])

    for job in jobs:
        try:
            project_id = str(job.project.identifier)
            network_id = str(job.network.identifier)
            flare_job_uuid = (
                job.flare_job_uuid
                or extract_flare_job_uuid(job.flare_job_id)
                or job.flare_job_id
            )

            network_workspace_root = os.path.join(
                settings.BASE_DIR, "workspaces", project_id, network_id
            )
            workspace_base = None
            for root, dirs, _ in os.walk(network_workspace_root):
                if "prod_00" in dirs:
                    workspace_base = os.path.join(root, "prod_00")
                    break

            if not workspace_base:
                continue

            # Step 1: Check Admin API status
            terminal_status = (
                job.status if job.status in {"COMPLETED", "FAILED", "STOPPED"} else None
            )
            admin_target = resolve_admin_session_target(job.network)
            if admin_target:
                try:
                    admin_name, admin_dir, server_ip = admin_target
                    sess = new_secure_session_with_host(
                        username=admin_name,
                        startup_kit_location=admin_dir,
                        host=server_ip,
                        timeout=10.0,
                        network_id=job.network.identifier,
                    )
                    resp = sess.api.do_command(f"list_jobs {flare_job_uuid}")
                    rjobs = parse_nvflare_jobs(resp)
                    if rjobs:
                        rstatus = str(
                            rjobs[0].get("status")
                            or rjobs[0].get("state")
                            or ""
                        ).upper()
                        if should_update_terminal_status(
                            terminal_status, rstatus
                        ):
                            terminal_status = rstatus
                    sess.close()
                except Exception:
                    pass

            # Step 2: Progress from logs
            if job.status in {"RUNNING", "COMPLETED"}:
                rounds_finished = job.rounds_finished or 0
                total_rounds = job.total_rounds or _get_total_rounds(
                    workspace_base
                )

                for root, _, files in os.walk(workspace_base):
                    if flare_job_uuid in root:
                        for fname in files:
                            if fname.startswith("log") and fname.endswith(
                                ".txt"
                            ):
                                try:
                                    with open(
                                        os.path.join(root, fname), "rb"
                                    ) as f:
                                        f.seek(0, os.SEEK_END)
                                        f.seek(
                                            max(0, f.tell() - 1024 * 512)
                                        )
                                        tail = f.read().decode(
                                            "utf-8", errors="ignore"
                                        )
                                        summary = summarize_training_log(tail)
                                        if summary["rounds_finished"] > rounds_finished:
                                            rounds_finished = summary["rounds_finished"]
                                        if should_update_terminal_status(
                                            terminal_status,
                                            summary["terminal_status"],
                                        ):
                                            terminal_status = summary[
                                                "terminal_status"
                                            ]
                                except Exception:
                                    pass

                if total_rounds > 0:
                    pct = progress_from_rounds(
                        rounds_finished,
                        total_rounds,
                        status=terminal_status or job.status,
                    )
                else:
                    pct = None

                training_services.persist_training_job_state(
                    job,
                    rounds_finished=rounds_finished,
                    total_rounds=total_rounds,
                    progress_percent=pct,
                    progress_updated_at=timezone.now(),
                )

            # Step 3: Result Sync
            if terminal_status == "COMPLETED":
                found_folders = []
                job_root = os.path.join(workspace_base, flare_job_uuid)
                if os.path.exists(job_root):
                    for item in os.listdir(job_root):
                        if item.startswith("app_"):
                            participant = item[4:]
                            found_folders.append(
                                (participant, os.path.join(job_root, item))
                            )

                if not found_folders:
                    for participant in os.listdir(workspace_base):
                        p_path = os.path.join(workspace_base, participant)
                        if os.path.isdir(
                            p_path
                        ) and participant.lower() not in [
                            "admin",
                            "startup",
                            "logs",
                            "local",
                            "transfer",
                            "custom",
                        ]:
                            target = os.path.join(p_path, flare_job_uuid)
                            if os.path.exists(target):
                                app_sub = os.path.join(
                                    target, f"app_{participant}"
                                )
                                found_folders.append(
                                    (
                                        participant,
                                        _select_result_root(target, app_sub),
                                    )
                                )

                if found_folders:
                    log.training.info(
                        f"Monitor: Syncing results for job {job.identifier}"
                    )
                    for participant, local_path in found_folders:
                        s3_prefix = f"{project_id}/results/{flare_job_uuid}/{participant}"
                        upload_folder_to_s3(
                            settings.AWS_STORAGE_BUCKET_NAME,
                            local_path,
                            s3_prefix,
                        )

                    training_services.persist_training_job_state(
                        job,
                        status=terminal_status,
                        completed_at=timezone.now(),
                        progress_percent=100,
                    )
            elif should_update_terminal_status(job.status, terminal_status):
                training_services.persist_training_job_state(
                    job,
                    status=terminal_status,
                    completed_at=timezone.now(),
                    progress_percent=job.progress_percent,
                )
        except Exception as e:
            log.training.error(
                f"Monitor: Error monitoring job {job.identifier}: {e}",
                extra=format_exception(e),
            )
