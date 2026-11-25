"""Celery background tasks for the training application.

Handles periodic monitoring of active training jobs, detection of completion,
and synchronization of result files (weights/logs) to S3 storage.
"""

import contextlib
import os
import uuid

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from celery import shared_task
from common.utils import get_s3_client
from logs import logger
from logs.utils import format_exception
from network.models import SwarmNetwork

from . import services as training_services
from .models import TrainingJob
from .resume import read_run_total
from .runtime import (
    admin_session,
    new_secure_session_with_host,
    parse_nvflare_jobs,
    resolve_admin_session_target,
    tail_text,
)
from .stall import describe_stall, same_stall, stall_warning_minutes
from .utils import (
    ClientDropouts,
    extract_flare_job_uuid,
    progress_from_rounds,
    scan_client_dropouts,
    summarize_training_log,
    should_update_terminal_status,
    upload_folder_to_s3,
)

# How much of the end of each log the monitor reads.
LOG_TAIL_BYTES = 512 * 1024

# How long a pass of the job monitor may hold its lock. A pass that dies without
# releasing it, such as one in a worker that was killed, delays the next passes
# by at most this long.
MONITOR_LOCK_SECONDS = 10 * 60


def _results_already_uploaded(project_id, flare_job_uuid):
    """Return True when object storage already holds result files for this job."""
    response = get_s3_client().list_objects_v2(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Prefix=f"{project_id}/results/{flare_job_uuid}/",
        MaxKeys=1,
    )
    return bool(response.get("KeyCount") or response.get("Contents"))


def _completion_time(job, terminal_status):
    """Completion time to record: now on the transition, else keep the stored one."""
    if job.status == terminal_status and job.completed_at:
        return job.completed_at
    return timezone.now()


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


def _server_log_paths(workspace_base: str) -> list[str]:
    """Return the FL server's own logs (log.txt of its workspace) under prod_00.

    The server writes its notices about sites that dropped out of a job there,
    not in the job's folder. Only a Hub that runs the server has them.
    """
    candidates = [workspace_base]
    with contextlib.suppress(OSError):
        candidates.extend(
            entry.path for entry in os.scandir(workspace_base) if entry.is_dir()
        )
    return [
        os.path.join(path, "log.txt")
        for path in candidates
        if os.path.isfile(os.path.join(path, "startup", "fed_server.json"))
        and os.path.isfile(os.path.join(path, "log.txt"))
    ]


def _update_stall_warning(job, dropouts, *, previous, threshold_minutes, log):
    """Set or clear the stall warning of a job that is still running.

    The warning is saved only while the job's row is still RUNNING, so that a
    job stopped or ended during this pass does not get one.

    Args:
        job (TrainingJob): The running job, with the state persisted in this pass.
        dropouts (ClientDropouts): What the job's logs at this Hub say about sites
            that dropped out.
        previous (str): The job's warning before this pass.
        threshold_minutes (float): ``SWARMMEDHUB_STALL_WARNING_MINUTES``.
        log: The platform logger.
    """
    warning = describe_stall(
        dropouts=dropouts.dropped(),
        last_round_at=job.last_round_at,
        now=timezone.now(),
        threshold_minutes=threshold_minutes,
    )
    if same_stall(previous, warning):
        warning = previous
    if warning != job.stall_warning:
        # No audit entry, as for any save of progress fields (common.signals).
        if not TrainingJob.objects.filter(pk=job.pk, status="RUNNING").update(
            stall_warning=warning
        ):
            return
        job.stall_warning = warning
    if warning and warning != previous:
        log.training.warning(
            f"Training job {job.identifier} has stalled: {warning}",
            project=job.project,
            network=job.network,
        )
    elif previous and not warning:
        log.training.info(
            f"Training job {job.identifier} is no longer stalled.",
            project=job.project,
            network=job.network,
        )


def _monitor_lock_key() -> str:
    # One lock per database: a test run beside a live Hub can use the Hub's
    # cache, but the test runner gives it a database of its own.
    return f"training:monitor_training_jobs:{settings.DATABASES['default']['NAME']}"


@shared_task
def monitor_training_jobs():
    """Periodic task to monitor training jobs and sync results.

    Discovers new jobs from the FLARE server, scrapes local Docker logs for
    real-time progress, warns about running jobs that have stalled (see
    :mod:`training.stall`), and automatically uploads finalized results to S3.

    One pass at a time: a pass that is due while another one runs, in any worker
    process, returns at once. The lock is held in the cache, which all worker
    processes and containers share.
    """
    key, token = _monitor_lock_key(), uuid.uuid4().hex
    try:
        locked = cache.add(key, token, MONITOR_LOCK_SECONDS)
    except Exception:
        locked = None  # the cache cannot be reached: run without the lock
    if locked is False:
        logger.get_logger().training.debug(
            "Monitor: the previous pass is still running; skipping this one."
        )
        return
    try:
        _monitor_pass()
    finally:
        if locked:
            with contextlib.suppress(Exception):
                if cache.get(key) == token:
                    cache.delete(key)


def _monitor_pass():
    """One pass of :func:`monitor_training_jobs`."""
    from .utils import scrape_docker_progress

    log = logger.get_logger()
    stall_minutes = stall_warning_minutes(log)
    # The warnings shown before this pass: the steps before Step 4 clear a job's
    # warning about rounds when its rounds advance, and Step 4 decides whether it
    # still holds.
    shown_warnings = dict(
        TrainingJob.objects.filter(status="RUNNING").values_list(
            "pk", "stall_warning"
        )
    )

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
            with admin_session(
                new_secure_session_with_host(
                    username=admin_name,
                    startup_kit_location=admin_dir,
                    host=server_ip,
                    timeout=10.0,
                    network_id=network.identifier,
                )
            ) as sess:
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
        except Exception:
            continue

    # 2. LOCAL DOCKER SCRAPING: Check running containers for progress: the FL
    # containers this Hub runs for the network, by the network's label, so that
    # a Hub that knows none of its addresses records the job its client runs.
    for network in active_networks:
        try:
            docker_results = scrape_docker_progress(network_id=network.identifier)

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
            previous_warning = shown_warnings.get(job.pk, job.stall_warning)
            dropouts = None
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
            # Finished jobs are not polled: their server is usually gone, and the
            # session retries every candidate port, which on a shared host reaches
            # other servers.
            admin_target = (
                resolve_admin_session_target(job.network)
                if terminal_status is None
                else None
            )
            if admin_target:
                try:
                    admin_name, admin_dir, server_ip = admin_target
                    with admin_session(
                        new_secure_session_with_host(
                            username=admin_name,
                            startup_kit_location=admin_dir,
                            host=server_ip,
                            timeout=10.0,
                            network_id=job.network.identifier,
                        )
                    ) as sess:
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
                except Exception:
                    pass

            # Step 2: Progress from logs (a finished job's progress no longer changes)
            if job.status == "RUNNING":
                # -1 until a round has finished, so that finishing round 0 counts
                # as progress and moves last_round_at.
                rounds_finished = (
                    job.rounds_finished if job.rounds_finished is not None else -1
                )
                # The job's own total, or none: another job's settings or a
                # guess would be stored as this job's.
                total_rounds = job.total_rounds or read_run_total(
                    os.path.join(network_workspace_root, "workspace"), flare_job_uuid
                )
                dropouts = ClientDropouts()

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
                                            max(0, f.tell() - LOG_TAIL_BYTES)
                                        )
                                        tail = f.read().decode(
                                            "utf-8", errors="ignore"
                                        )
                                        summary = summarize_training_log(
                                            tail, flare_job_uuid
                                        )
                                        if summary["rounds_finished"] > rounds_finished:
                                            rounds_finished = summary["rounds_finished"]
                                        if should_update_terminal_status(
                                            terminal_status,
                                            summary["terminal_status"],
                                        ):
                                            terminal_status = summary[
                                                "terminal_status"
                                            ]
                                        dropouts.merge(summary.get("client_dropouts"))
                                except Exception:
                                    pass

                # The server's notices name the job; they are in its own log.
                for server_log in _server_log_paths(workspace_base):
                    dropouts.merge(
                        scan_client_dropouts(
                            tail_text(server_log, LOG_TAIL_BYTES),
                            flare_job_uuid,
                            job_log=False,
                        )
                    )

                if total_rounds:
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

            # Step 3: Result Sync, once: when the job finishes in this pass, or for a
            # job marked complete elsewhere whose results never reached object
            # storage. Re-uploading every finished job each minute also overwrote
            # its completion time.
            needs_sync = terminal_status == "COMPLETED" and (
                job.status != "COMPLETED"
                or not _results_already_uploaded(project_id, flare_job_uuid)
            )
            if needs_sync:
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
                        completed_at=_completion_time(job, terminal_status),
                        progress_percent=100,
                    )
            elif should_update_terminal_status(job.status, terminal_status):
                training_services.persist_training_job_state(
                    job,
                    status=terminal_status,
                    completed_at=_completion_time(job, terminal_status),
                    progress_percent=job.progress_percent,
                )

            # Step 4: Stall warning, for a job that is still running; a job that
            # ended in this pass lost its warning with its new status.
            if dropouts is not None and job.status == "RUNNING":
                _update_stall_warning(
                    job,
                    dropouts,
                    previous=previous_warning,
                    threshold_minutes=stall_minutes,
                    log=log,
                )
        except Exception as e:
            log.training.error(
                f"Monitor: Error monitoring job {job.identifier}: {e}",
                extra=format_exception(e),
            )
