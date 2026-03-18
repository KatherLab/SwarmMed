"""Celery background tasks for the training application.

Handles periodic monitoring of active training jobs, detection of completion,
and synchronization of result files (weights/logs) to S3 storage.
"""

import contextlib
import json
import os
import re

from django.conf import settings
from django.utils import timezone

from celery import shared_task
from logs import logger
from logs.utils import format_exception
from network.models import SwarmNetwork

from .models import TrainingJob
from .utils import upload_folder_to_s3


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
            try:
                with open(cfg_path) as f:
                    cfg = json.load(f)
                for workflow in cfg.get("workflows", []):
                    if workflow.get("id") == "swarm_controller":
                        return int(
                            workflow.get("args", {}).get("num_rounds", 10)
                        )
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                json.JSONDecodeError,
            ):
                continue
    return 10


@shared_task
def monitor_training_jobs():
    """Periodic task to monitor training jobs and sync results.

    Discovers new jobs from the FLARE server, scrapes local Docker logs for 
    real-time progress, and automatically uploads finalized results to S3.
    """
    from network.utils import get_tailscale_ip

    from .utils import scrape_docker_progress
    from .views import (
        _parse_nvflare_jobs,
        _resolve_admin_session_target,
        new_secure_session_with_host,
    )

    local_ip = get_tailscale_ip()
    log = logger.get_logger()

    # 1. DISCOVERY: Find jobs on the FLARE server that aren't in our local database.
    active_networks = SwarmNetwork.objects.filter(
        status__in=["RUNNING", "STARTING", "PROVISIONED"]
    )

    for network in active_networks:
        admin_target = _resolve_admin_session_target(network)
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
            remote_jobs = _parse_nvflare_jobs(response)

            existing_job_ids = list(
                TrainingJob.objects.filter(network=network).values_list(
                    "flare_job_id", flat=True
                )
            )

            for rj in remote_jobs:
                job_id = str(rj.get("job_id") or rj.get("id") or "")
                if not job_id:
                    continue

                # Check for existing match (exact or substring)
                already_exists = False
                for ex_id in existing_job_ids:
                    if job_id in str(ex_id) or str(ex_id) in job_id:
                        already_exists = True
                        break

                if already_exists:
                    continue

                status = str(
                    rj.get("status") or rj.get("state") or "RUNNING"
                ).upper()
                log.training.info(
                    f"Monitor: Discovered new remote job: {job_id} ({status})"
                )
                TrainingJob.objects.create(
                    project=network.project,
                    network=network,
                    flare_job_id=job_id,
                    status=(
                        status
                        if status in ["RUNNING", "COMPLETED", "FAILED"]
                        else "RUNNING"
                    ),
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
                    l_job = TrainingJob.objects.filter(
                        network=network, flare_job_id=job_id
                    ).first()
                    if not l_job:
                        log.training.info(
                            f"Monitor: Creating local mirror for job {job_id} found in docker logs"
                        )
                        l_job = TrainingJob.objects.create(
                            project=network.project,
                            network=network,
                            flare_job_id=job_id,
                            status="RUNNING",
                        )

                    if res["rounds_finished"] >= 0:
                        if (
                            l_job.rounds_finished is None
                            or res["rounds_finished"] > l_job.rounds_finished
                        ):
                            log.training.info(
                                f"Monitor: Updating job {job_id} progress to round {res['rounds_finished']}"
                            )
                            l_job.rounds_finished = res["rounds_finished"]
                            total_rounds = l_job.total_rounds or _get_total_rounds(
                                os.path.join(
                                    settings.BASE_DIR,
                                    "workspaces",
                                    str(l_job.project.identifier),
                                    str(l_job.network.identifier),
                                )
                            )
                            l_job.progress_percent = min(
                                99,
                                int(
                                    (l_job.rounds_finished + 1)
                                    * 100
                                    / total_rounds
                                ),
                            )
                            l_job.progress_updated_at = timezone.now()
                            l_job.save(
                                update_fields=[
                                    "rounds_finished",
                                    "progress_percent",
                                    "progress_updated_at",
                                ]
                            )

                    if res["ended"] and l_job.status == "RUNNING":
                        log.training.info(
                            f"Monitor: Job {job_id} marked as COMPLETED via local docker logs"
                        )
                        l_job.status = "COMPLETED"
                        l_job.progress_percent = 100
                        l_job.completed_at = timezone.now()
                        l_job.save(
                            update_fields=[
                                "status",
                                "progress_percent",
                                "completed_at",
                            ]
                        )
        except Exception:
            continue

    # 3. MONITORING: Check RUNNING jobs (filesystem logs).
    jobs = TrainingJob.objects.filter(status__in=["RUNNING", "COMPLETED"])

    for job in jobs:
        try:
            project_id = str(job.project.identifier)
            network_id = str(job.network.identifier)
            flare_job_uuid = job.flare_job_id

            match = re.search(r"([0-9a-f-]{36})", str(job.flare_job_id))
            if match:
                flare_job_uuid = match.group(1)

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
            remote_finished = job.status == "COMPLETED"
            if not remote_finished:
                admin_target = _resolve_admin_session_target(job.network)
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
                        resp = sess.api.do_command(
                            f"list_jobs {flare_job_uuid}"
                        )
                        rjobs = _parse_nvflare_jobs(resp)
                        if rjobs:
                            rstatus = str(
                                rjobs[0].get("status")
                                or rjobs[0].get("state")
                                or ""
                            ).upper()
                            if rstatus in ["COMPLETED", "FAILED", "STOPPED"]:
                                remote_finished = True
                        sess.close()
                    except Exception:
                        pass

            # Step 2: Progress from logs
            ended = remote_finished
            if job.status == "RUNNING":
                rounds_finished = 0
                total_rounds = job.total_rounds or _get_total_rounds(
                    workspace_base
                )

                round_patterns = [
                    re.compile(r"Finished round\s+(\d+)", re.I),
                    re.compile(r"Round\s+(\d+)\s+\|", re.I),
                    re.compile(r"Round:\s+(\d+)", re.I),
                    re.compile(r"finished training round\s+(\d+)", re.I),
                ]

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

                                        completion_markers = [
                                            "ending workflow",
                                            "child worker process finished",
                                            "MPM: Good Bye!",
                                        ]
                                        if not ended and any(
                                            m in tail
                                            for m in completion_markers
                                        ):
                                            ended = True

                                        for pattern in round_patterns:
                                            for m in pattern.finditer(tail):
                                                rnum = int(m.group(1))
                                                if rnum > rounds_finished:
                                                    rounds_finished = rnum
                                except Exception:
                                    pass

                if total_rounds > 0:
                    rounds_completed = (
                        rounds_finished + 1 if rounds_finished >= 0 else 0
                    )
                    pct = int(rounds_completed * 100 / total_rounds)
                    job.progress_percent = 100 if ended else min(99, pct)

                job.rounds_finished = rounds_finished
                job.progress_updated_at = timezone.now()
                job.save(
                    update_fields=[
                        "rounds_finished",
                        "progress_percent",
                        "progress_updated_at",
                    ]
                )

            # Step 3: Result Sync
            if ended:
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
                                if os.path.exists(app_sub):
                                    found_folders.append(
                                        (participant, app_sub)
                                    )
                                else:
                                    found_folders.append((participant, target))

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

                    if job.status != "COMPLETED":
                        job.status = "COMPLETED"
                        job.completed_at = timezone.now()
                        job.progress_percent = 100
                        job.save(
                            update_fields=[
                                "status",
                                "completed_at",
                                "progress_percent",
                            ]
                        )
        except Exception as e:
            log.training.error(
                f"Monitor: Error monitoring job {job.identifier}: {e}",
                extra=format_exception(e),
            )
