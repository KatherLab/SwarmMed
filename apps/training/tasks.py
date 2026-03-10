"""
Celery background tasks for the training application.
Handles periodic monitoring of active training jobs, detection of completion,
and synchronization of result files (weights/logs) to S3 storage.
"""

import ast
import json
import os
import re
import shutil

from celery import shared_task
from django.conf import settings
from django.utils import timezone
from logs import logger
from logs.utils import format_exception
from network.models import SwarmNetwork

from .models import TrainingJob
from .utils import upload_folder_to_s3


def _get_total_rounds(workspace_base):
    """
    Helper to extract the configured number of training rounds from the 
    NVFlare server configuration file in the job definition.
    """
    cfg_path = os.path.join(
        workspace_base,
        "app_server",
        "config",
        "config_fed_server.json",
    )
    try:
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
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
    Periodic task to monitor training jobs, discover new ones from the FLARE server,
    and automatically upload finalized results to S3.
    """
    from .views import (
        _parse_nvflare_jobs,
        _resolve_admin_session_target,
        new_secure_session_with_host,
    )

    log = logger.get_logger()

    # 1. DISCOVERY: Find jobs on the FLARE server that aren't in our local database.
    # This ensures that if a job was started on PC2, PC3 will eventually "see" it.
    active_networks = SwarmNetwork.objects.filter(status__in=["RUNNING", "STARTING"])
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
                network_id=network.identifier
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
                
                status = str(rj.get("status") or rj.get("state") or "RUNNING").upper()
                TrainingJob.objects.create(
                    project=network.project,
                    network=network,
                    flare_job_id=job_id,
                    status=status if status in ["RUNNING", "COMPLETED", "FAILED"] else "RUNNING"
                )
                log.training.info(f"Discovered new remote job: {job_id} for network {network.name}")

            try:
                sess.close()
            except Exception:
                pass
        except Exception as e:
            log.training.debug(f"Job discovery failed for network {network.name}: {e}")

    # 2. MONITORING: Check RUNNING jobs to see if they finished.
    # Also check COMPLETED jobs that might need a final sync if started elsewhere.
    jobs = TrainingJob.objects.filter(status__in=["RUNNING", "COMPLETED"])

    for job in jobs:
        try:
            project_id = str(job.project.identifier)
            network_id = str(job.network.identifier)
            flare_job_id_raw = job.flare_job_id

            # Handle UUID extraction
            flare_job_uuid = flare_job_id_raw
            match = re.search(r"([0-9a-f-]{36})", flare_job_id_raw)
            if match:
                flare_job_uuid = match.group(1)

            log.training.info(
                f"Monitoring Job {job.identifier}. Flare UUID: {flare_job_uuid}"
            )

            # Step 1: Check status via Admin API if not already terminal
            remote_finished = (job.status == "COMPLETED")
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
                            network_id=job.network.identifier
                        )
                        resp = sess.api.do_command(f"list_jobs {flare_job_uuid}")
                        rjobs = _parse_nvflare_jobs(resp)
                        if rjobs:
                            rstatus = str(rjobs[0].get("status") or rjobs[0].get("state") or "").upper()
                            if rstatus in ["COMPLETED", "FAILED", "STOPPED"]:
                                remote_finished = True
                        sess.close()
                    except Exception:
                        pass

            # Step 2: Locate the NVFlare workspace on the local filesystem.
            network_workspace_root = os.path.join(
                settings.BASE_DIR, "workspaces", project_id, network_id
            )
            
            # Use unified project naming
            from common.utils import get_safe_slug
            project_name = get_safe_slug(job.project.title, job.project.identifier).replace("-", "_")
            workspace_base = os.path.join(
                network_workspace_root, "workspace", project_name, "prod_00"
            )

            if not os.path.exists(workspace_base):
                # Fallback scan for prod_00
                found = False
                for root, dirs, _ in os.walk(network_workspace_root):
                    if "prod_00" in dirs:
                        workspace_base = os.path.join(root, "prod_00")
                        found = True
                        break
                if not found:
                    continue

            # Step 3: Parse logs to calculate detailed progress (only for RUNNING)
            ended = remote_finished
            if job.status == "RUNNING":
                rounds_finished = 0
                total_rounds = job.total_rounds or 10

                try:
                    # Robust log scanning
                    # Patterns: "Finished round 0", "Round 0 | Epoch 1", "Round: 0"
                    round_patterns = [
                        re.compile(r"Finished round\s+(\d+)", re.I),
                        re.compile(r"Round\s+(\d+)\s+\|", re.I),
                        re.compile(r"Round:\s+(\d+)", re.I),
                        re.compile(r"finished training round\s+(\d+)", re.I),
                    ]
                    
                    # Search for logs anywhere in the workspace that mention the job UUID
                    for root, _, files in os.walk(workspace_base):
                        if flare_job_uuid in root:
                            for fname in files:
                                if fname.startswith("log") and fname.endswith(".txt"):
                                    try:
                                        with open(os.path.join(root, fname), "rb") as f:
                                            f.seek(0, os.SEEK_END)
                                            # Read a large chunk to catch recent progress
                                            chunk_size = 1024 * 250
                                            f.seek(max(0, f.tell() - chunk_size))
                                            tail = f.read().decode("utf-8", errors="ignore")
                                            
                                            # Check for completion markers
                                            completion_markers = [
                                                "ending workflow",
                                                "child worker process finished",
                                                "MPM: Good Bye!",
                                                "Training finished",
                                                "Training completed"
                                            ]
                                            if not ended and any(m in tail for m in completion_markers):
                                                ended = True
                                            
                                            for pattern in round_patterns:
                                                for m in pattern.finditer(tail):
                                                    rnum = int(m.group(1))
                                                    if rnum > rounds_finished:
                                                        rounds_finished = rnum
                                    except Exception:
                                        pass

                    if total_rounds > 0:
                        # Rounds are 0-indexed in logs usually, so finished 0 means we are on round 1
                        rounds_completed = rounds_finished + 1 if rounds_finished >= 0 else 0
                        pct = int(rounds_completed * 100 / total_rounds)
                        job.progress_percent = max(0, min(100, pct))
                    
                    job.rounds_finished = rounds_finished
                    job.progress_updated_at = timezone.now()
                    job.save(update_fields=["rounds_finished", "progress_percent", "progress_updated_at"])
                except Exception as e:
                    log.training.debug(f"Progress parse failed: {e}")

            # Step 4: If the job is complete, upload participant results to S3.
            if ended:
                log.training.info(
                    f"Job {job.identifier} ({flare_job_uuid}) is terminal. Checking for results sync..."
                )

                found_folders = []
                
                # 1. Standard NVFlare 2.7.1: prod_00/<job_uuid>/app_<participant_name>/
                job_root = os.path.join(workspace_base, flare_job_uuid)
                if os.path.exists(job_root):
                    for item in os.listdir(job_root):
                        if item.startswith("app_"):
                            participant = item[4:]
                            local_path = os.path.join(job_root, item)
                            found_folders.append((participant, local_path))

                # 2. Server-side/Hybrid: prod_00/<participant>/<job_uuid>/app_<participant>/
                if not found_folders:
                    for participant in os.listdir(workspace_base):
                        p_path = os.path.join(workspace_base, participant)
                        if os.path.isdir(p_path) and participant.lower() not in ["admin", "overseer", "startup", "local", "transfer", "logs"]:
                            job_p_path = os.path.join(p_path, flare_job_uuid)
                            if os.path.exists(job_p_path):
                                # Check for app_ subfolder inside
                                app_sub = f"app_{participant}"
                                if os.path.exists(os.path.join(job_p_path, app_sub)):
                                    found_folders.append((participant, os.path.join(job_p_path, app_sub)))
                                else:
                                    # Fallback to the job UUID folder itself
                                    found_folders.append((participant, job_p_path))

                if found_folders:
                    log.training.info(f"Found {len(found_folders)} result folders for upload.")
                    for participant, local_path in found_folders:
                        # Ensure we don't upload the 'startup' folder inside result apps
                        # S3 path structure: <project>/results/<job_uuid>/<client_name>/
                        s3_prefix = f"{project_id}/results/{flare_job_uuid}/{participant}"
                        upload_folder_to_s3(settings.AWS_STORAGE_BUCKET_NAME, local_path, s3_prefix)

                    if job.status != "COMPLETED":
                        job.status = "COMPLETED"
                        job.completed_at = timezone.now()
                        job.progress_percent = 100
                        job.save(update_fields=["status", "completed_at", "progress_percent"])
                    log.training.info(f"Job {job.identifier} successfully synced to S3.")
                else:
                    log.training.debug(f"No result folders found for {flare_job_uuid} in {workspace_base}")

        except Exception as e:
            log.training.error(
                f"Error monitoring job {job.identifier}: {str(e)}",
                extra=format_exception(e),
            )
