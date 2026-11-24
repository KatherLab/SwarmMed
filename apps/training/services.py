"""Shared training services used by the UI and CLI."""

from __future__ import annotations

import ast
import contextlib
import json
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, models, transaction
from django.utils import timezone

from common.utils import get_safe_slug
from logs.logger import get_logger
from network.failover import flare_admin_timeout_from_env, previous_server_host
from network.models import SwarmNetwork
from project.services import require_project_access, user_can_access_project
from results.artifacts import (
    GLOBAL_MODEL_FILENAME_CANDIDATES,
    is_model_artifact,
)

from .models import TrainingJob
from .resume import (
    FLARE_JOB_ID_RE,
    JOB_ID_PREFIX_RE,
    ResumeCheckpoint,
    ResumePlan,
    add_resume_checkpoint_component,
    checkpoint_metadata,
    describe_given_checkpoint,
    find_resume_checkpoints,
    job_folders,
    newest_round_start_job,
    read_run_plan,
    resume_checkpoints_enabled,
    ship_resume_checkpoint,
    write_run_file,
)
from .runtime import (
    build_training_status_payload,
    dedupe_keep_order,
    get_training_progress_info,
    log_flare_pre_submit_diagnostics,
    new_secure_session_with_host,
    parse_nvflare_clients,
    resolve_admin_session_target,
)
from .stall import is_site_warning
from .utils import (
    TERMINAL_TRAINING_STATUSES,
    clamp_progress_percent,
    download_s3_folder,
    extract_flare_job_uuid,
    progress_from_rounds,
    should_update_terminal_status,
)


# Environment overrides for the swarm workflow's server-side time limits. NVFlare's
# defaults are 90 s without a status report from a site, 3600 s without progress and
# 10 s for the starting site to start the workflow; each one aborts the whole job,
# which large or slow consortia can hit.
SWARM_TIME_LIMIT_ENV = {
    "max_status_report_interval": "SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL",
    "progress_timeout": "SWARMMEDHUB_SWARM_PROGRESS_TIMEOUT",
    "start_task_timeout": "SWARMMEDHUB_SWARM_START_TASK_TIMEOUT",
}

# NVFlare uses start_task_timeout as the timeout of a Task, which must be an int.
_WHOLE_SECOND_TIME_LIMITS = frozenset({"start_task_timeout"})

# start_task_timeout of a resumed job unless SWARMMEDHUB_SWARM_START_TASK_TIMEOUT is
# set. The starting site confirms the start of the workflow only once it has sent
# the first learn task to every other site. In a new run that task carries no model,
# and the start took 1 to 2.4 s in a test across three hospitals, well within
# NVFlare's 10 s. In a resumed run it carries the checkpoint: in the same test the
# starting site took 110 s to send a 94 MB checkpoint to the two other sites, so the
# 10 s default ended the run before it started.
RESUME_START_TASK_TIMEOUT = 600

# job_status_check_interval of a resumed job. FLARE checks each site's last status
# report job_status_check_interval seconds after the workflow has started (2 s by
# default) and ends the job if a site has not reported for max_status_report_interval
# (90 s in NVIDIA FLARE 2.7.1), counted from the start of the job. A site reports
# only once its first learn task has started, and the starting site sends no task
# request while it runs the start task. After a long start (a resumed job's start
# carries the checkpoint) the default check can therefore end the job before the
# starting site's first report arrives: in a test across three hospitals that report
# arrived 0.26 s before the check. A later first check leaves every site time to
# report; the status limit still applies for the rest of the job.
RESUME_JOB_STATUS_CHECK_INTERVAL = 10.0


def swarm_time_limits_from_env(log=None):
    """Return SwarmServerController time-limit overrides set in the environment.

    The values are seconds, positive and finite: NVFlare accepts nan and inf,
    but a deadline of either never expires. ``start_task_timeout`` takes only
    whole seconds, as NVFlare requires: "120" and "120.0" both give 120, "120.5"
    is rejected.

    Args:
        log: Optional platform logger for rejected values.

    Returns:
        dict: Keyword arguments for SwarmServerController; empty when nothing is
        set, so NVFlare's defaults apply.
    """
    limits = {}
    for argument, env_name in SWARM_TIME_LIMIT_ENV.items():
        raw = os.getenv(env_name, "").strip()
        if not raw:
            continue
        whole_seconds = argument in _WHOLE_SECOND_TIME_LIMITS
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if (
            not math.isfinite(value)
            or value <= 0
            or (whole_seconds and not value.is_integer())
        ):
            if log is not None:
                expected = "whole number" if whole_seconds else "number"
                log.training.warning(
                    f"Ignoring {env_name}={raw!r}: "
                    f"expected a positive {expected} of seconds"
                )
            continue
        limits[argument] = int(value) if whole_seconds else value
    return limits


def swarm_time_limits_for_job(*, resumed: bool, log=None) -> tuple[dict, str]:
    """Return the SwarmServerController time limits of a new job.

    The overrides set in the environment apply to every job. A resumed job
    also waits RESUME_START_TASK_TIMEOUT seconds for the workflow to start,
    unless SWARMMEDHUB_SWARM_START_TASK_TIMEOUT is set, and FLARE checks its
    sites' status reports every RESUME_JOB_STATUS_CHECK_INTERVAL seconds.

    Args:
        resumed: Whether the job resumes an interrupted run.
        log: Optional platform logger for rejected values.

    Returns:
        tuple[dict, str]: Keyword arguments for SwarmServerController, empty
        when NVFlare's defaults apply, and a note for the log on the values that
        SwarmMed chose itself ("" if there are none).
    """
    limits = swarm_time_limits_from_env(log)
    if not resumed:
        return limits, ""
    notes = []
    if "start_task_timeout" not in limits:
        limits["start_task_timeout"] = RESUME_START_TASK_TIMEOUT
        notes.append(
            f"start_task_timeout={RESUME_START_TASK_TIMEOUT} s is the default for a "
            "resumed job, whose first learn task carries the checkpoint, and "
            "SWARMMEDHUB_SWARM_START_TASK_TIMEOUT overrides it"
        )
    limits["job_status_check_interval"] = RESUME_JOB_STATUS_CHECK_INTERVAL
    notes.append(
        f"job_status_check_interval={RESUME_JOB_STATUS_CHECK_INTERVAL:g} s lets "
        "every site report its status after the start of a resumed job before "
        "FLARE first checks the reports"
    )
    return limits, f" ({'; '.join(notes)})"


def write_swarm_time_limits(job_path, time_limits: dict) -> list[str]:
    """Write time limits into the swarm server controller of an exported job.

    FLARE's job export leaves out every controller argument that equals the
    default of the Hub's NVIDIA FLARE build (10 s, 90 s and 3 600 s in 2.7.1).
    The FL server builds the controller with the defaults of the FLARE build in
    its own image, which can differ (360 000 s in the worked example's image),
    so every limit chosen for the job is written into its configuration.

    Args:
        job_path: Folder of the exported job.
        time_limits: Keyword arguments for SwarmServerController.

    Returns:
        list[str]: The app folders that were changed.
    """
    if not time_limits:
        return []
    changed = []
    for config_path in sorted(
        Path(job_path).glob("app_*/config/config_fed_server.json")
    ):
        config = json.loads(config_path.read_text())
        controllers = [
            workflow
            for workflow in config.get("workflows", [])
            if isinstance(workflow, dict)
            and str(workflow.get("path", "")).endswith("SwarmServerController")
        ]
        if not controllers:
            continue
        for workflow in controllers:
            workflow.setdefault("args", {}).update(time_limits)
        config_path.write_text(json.dumps(config, indent=4))
        changed.append(config_path.parent.parent.name)
    return changed


FLARE_CONNECT_TIMEOUT_ENV = "SWARMMEDHUB_FLARE_CONNECT_TIMEOUT"


def flare_connect_timeout() -> float:
    """Return the seconds an admin session gets to log in to the FL server.

    20 s unless SWARMMEDHUB_FLARE_CONNECT_TIMEOUT holds a number. Used for
    submitting a job and for asking a server whether it still runs one; there
    the server also gets this long to answer.
    """
    timeout = 20.0
    env_timeout = os.getenv(FLARE_CONNECT_TIMEOUT_ENV, "").strip()
    if env_timeout:
        with contextlib.suppress(ValueError):
            timeout = float(env_timeout)
    return timeout


def resume_timeout_hint() -> str:
    """Return the note that ``swarmed training resume`` prints, or "".

    A resumed job ships its checkpoint to the starting site, and that site
    sends it on to every other site when the workflow starts. Over a slow link
    FLARE's deadlines for these two steps can expire: the FL server's
    ``admin_timeout`` and the workflow's ``start_task_timeout``.

    The note follows the submission, so it says what to change if the job
    fails: the admin timeout takes effect when the network at the Hub that runs
    the FL server restarts, which would end the job just submitted. A resumed
    job that fails before its first round leaves no checkpoint, so the note
    points to the interrupted job. The note is left out once
    SWARMMEDHUB_FLARE_ADMIN_TIMEOUT holds a valid value.
    """
    if flare_admin_timeout_from_env() is not None:
        return ""
    start_timeout = swarm_time_limits_for_job(resumed=True)[0]["start_task_timeout"]
    return (
        "A resumed job ships its checkpoint to the starting site, which sends it "
        "on to the other sites. If this job fails over a slow link, resume the "
        "interrupted job again after setting SWARMMEDHUB_FLARE_ADMIN_TIMEOUT at the "
        "Hub that runs the FL server (then restart the network there the same way "
        "as before, which ends running jobs) or raising "
        "SWARMMEDHUB_SWARM_START_TASK_TIMEOUT "
        f"(now {start_timeout} s); see Server failover in the documentation."
    )


def list_training_jobs(*, project=None, network=None):
    """List training jobs with optional project/network filtering."""
    queryset = TrainingJob.objects.select_related("project", "network")
    if project is not None:
        queryset = queryset.filter(project=project)
    if network is not None:
        queryset = queryset.filter(network=network)
    return queryset.order_by("-created_at")


def _matching_jobs(queryset, identifier: str):
    condition = models.Q(flare_job_id__icontains=identifier)
    # A record's own identifier is a UUID; anything else, such as the start of a
    # FLARE job ID, would make the database lookup fail.
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        condition |= models.Q(identifier=uuid.UUID(str(identifier)))
    flare_job_uuid = extract_flare_job_uuid(identifier) or (
        identifier if re.fullmatch(r"[0-9a-fA-F-]{36}", str(identifier or "")) else None
    )
    if flare_job_uuid:
        condition |= models.Q(flare_job_uuid=flare_job_uuid)
    return queryset.filter(condition)


def ensure_training_job(
    *, network: SwarmNetwork, flare_job_id: str, status: str = "RUNNING"
) -> tuple[TrainingJob, bool]:
    """Return the canonical local job row for a FLARE job identifier."""
    queryset = (
        TrainingJob.objects.select_related("project", "network")
        .filter(network=network)
        .order_by("created_at", "id")
    )
    flare_job_uuid = extract_flare_job_uuid(flare_job_id)
    job = None
    if flare_job_uuid:
        job = queryset.filter(flare_job_uuid=flare_job_uuid).first()
    if not job and flare_job_id:
        job = queryset.filter(flare_job_id=flare_job_id).first()
    if not job and flare_job_id:
        job = queryset.filter(flare_job_id__icontains=flare_job_id).first()

    created = False
    if not job:
        try:
            with transaction.atomic():
                job = TrainingJob.objects.create(
                    project=network.project,
                    network=network,
                    status=status,
                    flare_job_id=flare_job_id,
                    flare_job_uuid=flare_job_uuid,
                )
            created = True
        except IntegrityError:
            # The job monitor, the Training page or a command recorded the job
            # since the lookup above; there is one row per network and job.
            if not flare_job_uuid:
                raise
            job = TrainingJob.objects.get(
                network=network, flare_job_uuid=flare_job_uuid
            )
    else:
        update_fields = []
        if flare_job_uuid and job.flare_job_uuid != flare_job_uuid:
            job.flare_job_uuid = flare_job_uuid
            update_fields.append("flare_job_uuid")
        if flare_job_id and not job.flare_job_id:
            job.flare_job_id = flare_job_id
            update_fields.append("flare_job_id")
        if update_fields:
            job.save(update_fields=update_fields)
    return job, created


def persist_training_job_state(
    job: TrainingJob,
    *,
    flare_job_id: str | None = None,
    status: str | None = None,
    total_rounds: int | None = None,
    rounds_finished: int | None = None,
    progress_percent: int | None = None,
    completed_at=None,
    progress_updated_at=None,
    stall_warning: str | None = None,
) -> TrainingJob:
    """Persist one canonical training-job state transition.

    ``last_round_at`` is set when the job starts running and whenever
    ``rounds_finished`` increases. Unless the call sets a new stall warning for a
    running job, a warning about rounds is cleared when rounds advance. A warning
    about a site is left to the job monitor, which clears it once the site takes
    part again. Both are cleared when the job ends.
    """
    update_fields = []
    new_status = str(status or "").upper() or None

    if flare_job_id:
        flare_job_uuid = extract_flare_job_uuid(flare_job_id)
        if not job.flare_job_id:
            job.flare_job_id = flare_job_id
            update_fields.append("flare_job_id")
        if flare_job_uuid and job.flare_job_uuid != flare_job_uuid:
            job.flare_job_uuid = flare_job_uuid
            update_fields.append("flare_job_uuid")

    if total_rounds is not None and total_rounds > 0 and job.total_rounds != total_rounds:
        job.total_rounds = total_rounds
        update_fields.append("total_rounds")

    if (
        rounds_finished is not None
        and rounds_finished >= 0
        and (job.rounds_finished is None or rounds_finished > job.rounds_finished)
    ):
        job.rounds_finished = rounds_finished
        job.last_round_at = timezone.now()
        update_fields.extend(["rounds_finished", "last_round_at"])
        if stall_warning is None and not is_site_warning(job.stall_warning):
            stall_warning = ""  # a warning about rounds ends when rounds advance

    if new_status:
        current_status = str(job.status or "").upper()
        if should_update_terminal_status(current_status, new_status):
            if job.status != new_status:
                job.status = new_status
                update_fields.append("status")
            # Only a finished job has a completion time; a PENDING -> RUNNING update
            # used to stamp one at submission.
            if completed_at is None and new_status in TERMINAL_TRAINING_STATUSES:
                completed_at = timezone.now()
        elif current_status not in {"COMPLETED", "FAILED", "STOPPED"} and job.status != new_status:
            job.status = new_status
            update_fields.append("status")

    running = str(job.status or "").upper() == "RUNNING"
    if running and job.last_round_at is None:
        job.last_round_at = timezone.now()
        update_fields.append("last_round_at")
    if not running:
        stall_warning = ""  # only a running job can stall
    if stall_warning is not None and (
        job.stall_warning != stall_warning
        # A job that ends here loses its warning even if this copy has none: the
        # monitor may have set one since the copy was read.
        or (not running and "status" in update_fields)
    ):
        job.stall_warning = stall_warning
        update_fields.append("stall_warning")

    effective_status = str(job.status or new_status or "").upper()
    normalized_progress = progress_percent
    if normalized_progress is not None:
        normalized_progress = clamp_progress_percent(
            normalized_progress, status=effective_status
        )
    elif effective_status == "COMPLETED":
        normalized_progress = 100

    if normalized_progress is not None and job.progress_percent != normalized_progress:
        job.progress_percent = normalized_progress
        update_fields.append("progress_percent")
        if progress_updated_at is None:
            progress_updated_at = timezone.now()

    if completed_at is not None and job.completed_at != completed_at:
        job.completed_at = completed_at
        update_fields.append("completed_at")

    if progress_updated_at is not None and job.progress_updated_at != progress_updated_at:
        job.progress_updated_at = progress_updated_at
        update_fields.append("progress_updated_at")

    if update_fields:
        job.save(update_fields=list(dict.fromkeys(update_fields)))
    return job


def sync_job_from_docker_result(*, network: SwarmNetwork, result: dict) -> TrainingJob | None:
    """Merge one docker-scraped runtime result into the canonical job row."""
    job_id = result.get("job_id")
    if not job_id:
        running_job = (
            TrainingJob.objects.filter(network=network, status="RUNNING")
            .order_by("-created_at")
            .first()
        )
        if running_job:
            job_id = running_job.flare_job_id or running_job.flare_job_uuid
    if not job_id:
        return None

    job, _created = ensure_training_job(
        network=network,
        flare_job_id=str(job_id),
        status="RUNNING",
    )
    total_rounds = job.total_rounds or 10
    rounds_finished = result.get("rounds_finished")
    progress_percent = None
    if rounds_finished is not None and rounds_finished >= 0:
        progress_percent = progress_from_rounds(
            rounds_finished, total_rounds, status=result.get("terminal_status") or "RUNNING"
        )
    return persist_training_job_state(
        job,
        flare_job_id=str(job_id),
        rounds_finished=rounds_finished,
        total_rounds=total_rounds,
        progress_percent=progress_percent,
        progress_updated_at=timezone.now() if progress_percent is not None else None,
        status=result.get("terminal_status"),
    )


def sync_job_from_remote_payload(
    *, network: SwarmNetwork, remote_job: dict
) -> TrainingJob | None:
    """Merge one peer-mirrored job payload into the canonical job row."""
    flare_job_id = str(remote_job.get("flare_job_id") or "").strip()
    if not flare_job_id:
        return None

    job, _created = ensure_training_job(
        network=network,
        flare_job_id=flare_job_id,
        status=str(remote_job.get("status") or "RUNNING").upper(),
    )
    return persist_training_job_state(
        job,
        flare_job_id=flare_job_id,
        status=remote_job.get("status"),
        total_rounds=remote_job.get("total_rounds"),
        rounds_finished=remote_job.get("rounds_finished"),
        progress_percent=remote_job.get("progress_percent"),
        completed_at=timezone.now()
        if str(remote_job.get("status") or "").upper()
        in {"COMPLETED", "FAILED", "STOPPED"}
        else None,
    )


def sync_job_from_nvflare_status(
    *, network: SwarmNetwork, nvflare_status: dict
) -> TrainingJob | None:
    """Merge one NVFlare admin status payload into the canonical job row."""
    job_id_to_match = str(nvflare_status.get("job_id") or "").strip()
    if not job_id_to_match:
        return None

    status_map = {
        "RUNNING": "RUNNING",
        "COMPLETED": "COMPLETED",
        "STOPPED": "STOPPED",
        "FAILED": "FAILED",
        "SUBMITTED": "STARTING",
        "APPROVED": "STARTING",
        "DISPATCHED": "STARTING",
    }
    status_key = str(nvflare_status.get("status", "")).upper().strip()
    mapped_status = status_map.get(status_key)
    if not mapped_status:
        return None

    job, _created = ensure_training_job(
        network=network,
        flare_job_id=job_id_to_match,
        status=mapped_status,
    )
    progress_percent = (
        100 if mapped_status == "COMPLETED" else nvflare_status.get("progress")
    )
    return persist_training_job_state(
        job,
        flare_job_id=job_id_to_match,
        status=mapped_status,
        progress_percent=progress_percent,
        completed_at=timezone.now()
        if mapped_status in {"COMPLETED", "FAILED", "STOPPED"}
        else None,
    )


def get_training_job(*, network=None, identifier: str) -> TrainingJob:
    """Resolve a training job by UUID identifier or FLARE job id."""
    queryset = TrainingJob.objects.select_related("project", "network")
    if network is not None:
        queryset = queryset.filter(network=network)
    job = _matching_jobs(queryset, identifier).order_by("-created_at").first()
    if not job:
        raise LookupError(f"Training job '{identifier}' was not found.")
    return job


def get_latest_training_job(network: SwarmNetwork) -> TrainingJob | None:
    """Return the latest training job for a network."""
    return (
        TrainingJob.objects.filter(network=network)
        .select_related("project", "network")
        .order_by("-created_at")
        .first()
    )


def _isoformat(value) -> str | None:
    return value.isoformat() if value else None


def serialize_training_job(job: TrainingJob) -> dict:
    """Serialize a training job for CLI output."""
    info = get_training_progress_info(job, job.network)
    return {
        "identifier": str(job.identifier),
        "project_identifier": str(job.project.identifier),
        "network_identifier": str(job.network.identifier),
        "status": job.status,
        "display_status": info["status"],
        "progress_percent": info["progress"],
        "duration": info["duration"],
        "eta": info["eta"],
        "flare_job_id": job.flare_job_id,
        "flare_job_uuid": job.flare_job_uuid,
        "total_rounds": job.total_rounds,
        "rounds_finished": job.rounds_finished,
        "last_round_at": _isoformat(job.last_round_at),
        "stall_warning": job.stall_warning,
        "created_at": job.created_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def serialize_training_job_state(job: TrainingJob) -> dict:
    """Compact serialisation used by the inter-node training-state endpoint.

    Kept narrower than :func:`serialize_training_job` because the gossip
    protocol consumers only need the canonical identity + progress fields. It
    also gives ``last_round_at`` and ``stall_warning`` (only the Hub that runs
    the FL server can name a dropped-out site); the receiving Hubs do not merge
    them yet.
    """
    return {
        "flare_job_id": job.flare_job_id,
        "flare_job_uuid": job.flare_job_uuid,
        "status": job.status,
        "total_rounds": job.total_rounds,
        "rounds_finished": job.rounds_finished,
        "progress_percent": job.progress_percent,
        "last_round_at": _isoformat(job.last_round_at),
        "stall_warning": job.stall_warning,
        "created_at": job.created_at.isoformat(),
    }


# Filenames the workspace walker accepts as a "global model" artifact, in
# preference order. Matches what training_api_results historically returned;
# the canonical FL_global_model.pt is checked first, then the legacy
# alternatives so existing client nodes continue to download something useful.
_GLOBAL_MODEL_FILENAME_CANDIDATES = GLOBAL_MODEL_FILENAME_CANDIDATES


def find_local_global_model_file(job: TrainingJob) -> str | None:
    """Locate the most recent global-model file on the local filesystem.

    The lookup walks ``<BASE_DIR>/workspaces/<project>/<network>/workspace`` for
    a directory whose name contains the FLARE job UUID, then prefers filenames
    in :data:`_GLOBAL_MODEL_FILENAME_CANDIDATES`. Both Hub views and CLI
    helpers can call this so they agree on what counts as the latest model.
    Returns an absolute path, or ``None`` if nothing matches.
    """
    flare_id = job.flare_job_uuid or extract_flare_job_uuid(job.flare_job_id) or str(
        job.flare_job_id or ""
    )
    if not flare_id:
        return None

    workspace_root = Path(settings.BASE_DIR) / "workspaces" / str(
        job.project.identifier
    ) / str(job.network.identifier) / "workspace"
    if not workspace_root.exists():
        return None

    matched: list[Path] = []
    for root, _dirs, files in os.walk(workspace_root):
        if flare_id not in root:
            continue
        for filename in _GLOBAL_MODEL_FILENAME_CANDIDATES:
            candidate = Path(root) / filename
            if candidate.exists():
                matched.append(candidate)
        # Collect any other supported model artifact as a last-resort fallback.
        for name in files:
            if (
                is_model_artifact(name)
                and name not in _GLOBAL_MODEL_FILENAME_CANDIDATES
            ):
                matched.append(Path(root) / name)

    if not matched:
        return None

    # Preserve the candidate-list ordering so FL_global_model.pt wins over the
    # legacy alternatives even when both exist in the same directory tree.
    def _rank(path: Path) -> tuple[int, float]:
        try:
            preferred = _GLOBAL_MODEL_FILENAME_CANDIDATES.index(path.name)
        except ValueError:
            preferred = len(_GLOBAL_MODEL_FILENAME_CANDIDATES)
        # Newer mtime wins within the same preference bucket.
        return (preferred, -path.stat().st_mtime)

    matched.sort(key=_rank)
    return str(matched[0])


def _participant_for_artifact_path(path: Path, network: SwarmNetwork) -> str:
    """Infer a normalized participant id from a workspace artifact path."""
    parts = set(path.parts)
    for participant in network.participants.all():
        participant_id = participant.participant_id
        if participant_id in parts or f"app_{participant_id}" in parts:
            return participant_id
    return "local"


def _relative_artifact_path(
    path: Path, *, flare_id: str, participant_id: str
) -> str:
    """Return artifact path below the FLARE job folder, stripping app wrappers."""
    parts = list(path.parts)
    try:
        job_index = parts.index(flare_id)
    except ValueError:
        return path.name

    relative_parts = parts[job_index + 1 :]
    if relative_parts and relative_parts[0] == f"app_{participant_id}":
        relative_parts = relative_parts[1:]
    if relative_parts and relative_parts[0] == participant_id:
        relative_parts = relative_parts[1:]
    return "/".join(relative_parts) if relative_parts else path.name


def sync_completed_job_local_results(
    job: TrainingJob, *, network: SwarmNetwork | None = None
) -> list[str]:
    """Register local completed-job model artifacts in object storage.

    Returns the S3/storage keys that were found or saved. The canonical key
    layout is ``<project>/results/<flare_job_uuid>/<participant>/<artifact>``.
    Legacy callers can still read older non-participant keys through the
    results service parser.
    """
    network = network or job.network
    flare_id = job.flare_job_uuid or extract_flare_job_uuid(job.flare_job_id) or str(
        job.flare_job_id or ""
    )
    if not flare_id:
        return []

    workspace_root = Path(settings.BASE_DIR) / "workspaces" / str(
        job.project.identifier
    ) / str(network.identifier) / "workspace"
    if not workspace_root.exists():
        return []

    synced_keys: list[str] = []
    for root, _dirs, files in os.walk(workspace_root):
        if flare_id not in root:
            continue
        root_path = Path(root)
        participant_id = _participant_for_artifact_path(root_path, network)
        for filename in files:
            if not is_model_artifact(filename):
                continue
            local_path = root_path / filename
            relative_path = _relative_artifact_path(
                local_path,
                flare_id=flare_id,
                participant_id=participant_id,
            )
            s3_key = (
                f"{job.project.identifier}/results/{flare_id}/"
                f"{participant_id}/{relative_path}"
            )
            if not default_storage.exists(s3_key):
                with local_path.open("rb") as handle:
                    default_storage.save(s3_key, ContentFile(handle.read()))
            synced_keys.append(s3_key)
    return synced_keys


def _resolve_swarm_rounds(training_py_path: str, log) -> int:
    """Return the number of swarm rounds for a new job.

    ``SWARMMEDHUB_SWARM_ROUNDS`` wins over ``SWARM_ROUNDS = N`` in the training
    script; the default is 10.
    """
    swarm_rounds = 10
    try:
        env_swarm_rounds = os.getenv("SWARMMEDHUB_SWARM_ROUNDS", "").strip()
        if env_swarm_rounds:
            swarm_rounds = int(env_swarm_rounds)
            log.training.info(
                f"Using SWARMMEDHUB_SWARM_ROUNDS={swarm_rounds} from environment"
            )
        elif os.path.exists(training_py_path):
            with open(training_py_path) as handle:
                content = handle.read()
            match = re.search(r"SWARM_ROUNDS\s*=\s*(\d+)", content)
            if match:
                swarm_rounds = int(match.group(1))
                log.training.info(
                    f"Extracted SWARM_ROUNDS={swarm_rounds} from training script: {training_py_path}"
                )
            else:
                log.training.warning(
                    f"SWARM_ROUNDS not found in {training_py_path}, defaulting to 10"
                )
    except Exception as exc:
        log.training.warning(
            f"Failed to extract SWARM_ROUNDS from training script: {exc}"
        )
    return swarm_rounds


def submit_training_job(
    *, actor, network: SwarmNetwork, resume: ResumePlan | None = None
) -> TrainingJob:
    """Assemble and submit an NVFlare job for a running network.

    Args:
        actor: The user submitting the job.
        network: A running network.
        resume: Set to continue an interrupted run: the job starts at
            ``resume.start_round`` from ``resume.checkpoint`` and runs the
            rounds left of ``resume.total_rounds``. See :func:`resume_training_job`.
    """
    if network.status != "RUNNING":
        raise ValueError(
            f"Network '{network.name}' is not running. Start the network before training."
        )

    project = network.project
    log = get_logger(user=actor, project=project)
    job_dir = os.path.join(
        settings.BASE_DIR,
        "workspaces",
        str(project.identifier),
        str(network.identifier),
        "job",
    )
    project_name = get_safe_slug(project.title, project.identifier).replace(
        "-", "_"
    )
    admin_target = resolve_admin_session_target(network)
    if not admin_target:
        raise LookupError(
            "No admin startup kit found for this center. Re-provision or import a complete startup package."
        )

    admin_username, admin_session_dir, server_ip = admin_target
    app_server_dir = os.path.join(job_dir, "app_server")
    app_client_dir = os.path.join(job_dir, "app_client")
    app_client_custom_dir = os.path.join(app_client_dir, "custom")
    os.makedirs(os.path.join(app_server_dir, "config"), exist_ok=True)
    os.makedirs(os.path.join(app_client_dir, "config"), exist_ok=True)
    os.makedirs(app_client_custom_dir, exist_ok=True)

    source_code_prefix = f"{project.identifier}/code/training/"
    try:
        download_s3_folder(
            settings.AWS_STORAGE_BUCKET_NAME,
            source_code_prefix,
            app_client_custom_dir,
        )
        log.training.info(
            f"Downloaded training code from S3: {source_code_prefix}"
        )
    except Exception as exc:
        log.training.error(f"Failed to download training code: {exc}")

    flare_adapter_src = os.path.join(
        settings.BASE_DIR, "apps", "training", "flare_adapter.py"
    )
    shutil.copyfile(
        flare_adapter_src, os.path.join(app_client_custom_dir, "flare_adapter.py")
    )

    training_py_path = os.path.join(app_client_custom_dir, "training.py")
    if not os.path.exists(training_py_path):
        # Every executor below runs custom/training.py. Without it the clients have
        # nothing to run and the job ends immediately, so refuse to submit.
        raise ValueError(
            "No training.py found in the project's stored training code "
            f"({source_code_prefix}). Check that object storage is reachable and "
            "re-upload the project's code before starting training."
        )
    if os.path.exists(training_py_path):
        with open(training_py_path) as handle:
            content = handle.read()
        replacement = f'main(project_id="{str(project.identifier)}")'
        content = content.replace(
            'main(project_id="default_project")', replacement
        ).replace("main(project_id='default_project')", replacement)
        with open(training_py_path, "w") as handle:
            handle.write(content)

    client_names = list(
        network.participants.filter(role="CLIENT").values_list(
            "participant_id", flat=True
        )
    )
    if not client_names:
        workspace_root = os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(project.identifier),
            str(network.identifier),
        )
        try:
            prod_00_check = os.path.dirname(os.path.abspath(admin_session_dir))
            local_client_names_file = os.path.join(
                prod_00_check, ".local_client_names.json"
            )
            if os.path.exists(local_client_names_file):
                with open(local_client_names_file) as handle:
                    local_client_names = json.load(handle)
                if isinstance(local_client_names, list):
                    client_names.extend(
                        [str(name) for name in local_client_names if name]
                    )

            if not client_names:
                participants_file = os.path.join(
                    admin_session_dir, "startup", ".all_participants.json"
                )
                if os.path.exists(participants_file):
                    with open(participants_file) as handle:
                        all_participants = json.load(handle)
                    if isinstance(all_participants, dict) and "clients" in all_participants:
                        client_names.extend(
                            [
                                str(name)
                                for name in all_participants["clients"]
                                if name
                            ]
                        )
        except Exception:
            pass

        if not client_names:
            try:
                import yaml

                project_yml = os.path.join(workspace_root, "project.yml")
                if os.path.exists(project_yml):
                    with open(project_yml) as handle:
                        project_yaml = yaml.safe_load(handle) or {}
                    for participant in project_yaml.get("participants", []):
                        role = str(
                            participant.get("type", participant.get("role", ""))
                        ).lower()
                        name = str(participant.get("name", "")).strip()
                        if name and role in {"client", "fl_client"}:
                            client_names.append(name)
            except Exception:
                pass

        if not client_names:
            try:
                prod_00_dir = os.path.dirname(os.path.abspath(admin_session_dir))
                excluded = {
                    "server",
                    "admin_startup",
                    "startup",
                    "transfer",
                    "local",
                    "logs",
                    "custom",
                }
                for entry in sorted(os.scandir(prod_00_dir), key=lambda item: item.name):
                    if (
                        entry.is_dir()
                        and entry.name not in excluded
                        and not entry.name.startswith(".")
                        and os.path.isdir(os.path.join(entry.path, "startup"))
                    ):
                        client_names.append(entry.name)
            except Exception:
                pass

    client_names = dedupe_keep_order(
        [
            name
            for name in client_names
            if name and name.lower() not in ["server", "admin"]
        ]
    )

    server_names = list(
        network.participants.filter(role="SERVER").values_list(
            "participant_id", flat=True
        )
    )
    if "server" not in server_names:
        server_names = ["server"]

    framework = "np"
    if os.path.exists(training_py_path):
        with open(training_py_path) as handle:
            script_text = handle.read()
        try:
            tree = ast.parse(script_text)
            imported_modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module = (alias.name or "").split(".")[0].strip()
                        if module:
                            imported_modules.add(module)
                elif isinstance(node, ast.ImportFrom):
                    module = (node.module or "").split(".")[0].strip()
                    if module:
                        imported_modules.add(module)
            if imported_modules.intersection({"tensorflow", "keras"}):
                framework = "tf"
            elif imported_modules.intersection(
                {
                    "torch",
                    "pytorch_lightning",
                    "lightning",
                    "transformers",
                    "monai",
                }
            ):
                framework = "pt"
            elif imported_modules.intersection({"sklearn", "numpy", "pandas"}):
                framework = "np"
        except SyntaxError:
            if (
                "tensorflow" in script_text.lower()
                or "keras" in script_text.lower()
            ):
                framework = "tf"

    log.training.info(f"Detected training framework: {framework}")
    swarm_rounds = _resolve_swarm_rounds(training_py_path, log)
    if resume is not None:
        if framework != "pt":
            raise ValueError(
                "Only PyTorch training jobs can be resumed: the checkpoint is "
                f"loaded by FLARE's PyTorch persistor (detected framework: {framework})."
            )
        if not resume.total_rounds:
            # Never this Hub's own count: the run may have another one.
            raise ValueError("A resumed job needs the run's total number of rounds.")
        swarm_rounds = resume.total_rounds
        if resume.remaining_rounds <= 0:
            raise ValueError(
                f"Nothing to resume: the checkpoint starts at round {resume.start_round} "
                f"and the run has {resume.total_rounds} rounds."
            )
    if framework == "pt":
        runtime_requirements_path = os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(project.identifier),
            str(network.identifier),
            "runtime_requirements.txt",
        )
        has_torch_dependency = False
        if os.path.exists(runtime_requirements_path):
            try:
                with open(runtime_requirements_path) as handle:
                    for raw_line in handle:
                        line = raw_line.strip().lower()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("torch"):
                            has_torch_dependency = True
                            break
            except Exception as exc:
                log.training.warning(
                    f"Could not validate runtime requirements for PyTorch: {exc}"
                )
        if not has_torch_dependency:
            raise ValueError(
                "PyTorch training detected, but runtime requirements do not include 'torch'. "
                "Add torch to the project's requirements file, re-provision/start the network, and retry."
            )

    try:
        submit_connect_timeout = flare_connect_timeout()
        env_timeout = os.getenv(FLARE_CONNECT_TIMEOUT_ENV, "").strip()

        log.training.info(
            "FLARE submit timeout config: "
            f"requested_timeout={submit_connect_timeout}, "
            f"env_SWARMMEDHUB_FLARE_CONNECT_TIMEOUT={env_timeout or 'unset'}"
        )

        from nvflare.apis.dxo import DataKind
        from nvflare.app_common.aggregators.intime_accumulate_model_aggregator import (
            InTimeAccumulateWeightedAggregator,
        )
        from nvflare.app_common.ccwf import (
            SwarmClientController,
            SwarmServerController,
        )
        from nvflare.app_common.ccwf.comps.simple_intime_model_selector import (
            SimpleIntimeModelSelector,
        )
        from nvflare.app_common.ccwf.comps.simple_model_shareable_generator import (
            SimpleModelShareableGenerator,
        )
        from nvflare.app_common.executors.in_process_client_api_executor import (
            InProcessClientAPIExecutor,
        )
        from nvflare.job_config.api import FedJob

        executor = InProcessClientAPIExecutor(task_script_path="custom/training.py")

        if framework == "np":
            try:
                from nvflare.app_common.np.np_model_persistor import (
                    NPModelPersistor,
                )

                persistor = NPModelPersistor()
            except (ModuleNotFoundError, ImportError):
                from nvflare.app_opt.pt.file_model_persistor import (
                    PTFileModelPersistor,
                )

                persistor = PTFileModelPersistor()
        else:
            try:
                from nvflare.app_opt.pt.file_model_persistor import (
                    PTFileModelPersistor,
                )

                persistor = PTFileModelPersistor()
            except (ModuleNotFoundError, ImportError):
                from nvflare.app_common.np.np_model_persistor import (
                    NPModelPersistor,
                )

                persistor = NPModelPersistor()

        if resume is not None:
            from nvflare.app_opt.pt.file_model_persistor import PTFileModelPersistor

            # Keep the weights in the type the run exchanged. PyTorch scripts
            # (and NVFlare's Lightning callback) load tensors, not NumPy arrays.
            persistor = PTFileModelPersistor(
                allow_numpy_conversion=resume.checkpoint.weights_format == "numpy"
            )

        if framework == "tf":
            try:
                from nvflare.app_opt.tf.in_process_client_api_executor import (
                    TFInProcessClientAPIExecutor,
                )

                executor = TFInProcessClientAPIExecutor(
                    task_script_path="custom/training.py"
                )
            except (ModuleNotFoundError, ImportError):
                pass
        elif framework == "pt":
            use_pt_executor = (
                os.getenv("SWARMMEDHUB_ENABLE_PT_EXECUTOR", "")
                .strip()
                .lower()
                in {"1", "true", "yes", "on"}
            )
            if use_pt_executor:
                try:
                    from nvflare.app_opt.pt.in_process_client_api_executor import (
                        PTInProcessClientAPIExecutor,
                    )

                    executor = PTInProcessClientAPIExecutor(
                        task_script_path="custom/training.py"
                    )
                except (ModuleNotFoundError, ImportError):
                    pass

        shareable_generator = SimpleModelShareableGenerator()
        aggregator = InTimeAccumulateWeightedAggregator(
            expected_data_kind=DataKind.WEIGHTS
        )
        model_selector = SimpleIntimeModelSelector(
            validation_metric_name="accuracy"
        )

        log.training.info(
            "Selected NVFlare executor: "
            f"{executor.__class__.__module__}.{executor.__class__.__name__}"
        )

        log_flare_pre_submit_diagnostics(
            log=log,
            username=admin_username,
            startup_kit_location=admin_session_dir,
            requested_host=server_ip,
        )

        session = new_secure_session_with_host(
            username=admin_username,
            startup_kit_location=admin_session_dir,
            host=server_ip,
            timeout=submit_connect_timeout,
            network_id=network.identifier,
        )

        if not client_names or set(client_names).issubset(
            {"fl-client-1", "fl-client-2"}
        ):
            try:
                log.training.info("Querying live server for connected clients...")
                client_response = session.api.do_command("list_clients")
                live_clients = parse_nvflare_clients(client_response)
                if live_clients:
                    client_names = live_clients
                    log.training.info(
                        f"Discovered connected clients: {client_names}"
                    )
            except Exception as client_error:
                log.training.warning(
                    f"Live client discovery failed: {client_error}"
                )

        if not client_names:
            client_names = ["fl-client-1", "fl-client-2"]

        job_definition = FedJob(name=f"{project_name}_job")
        private_p2p = (
            os.getenv("SWARMMEDHUB_PRIVATE_P2P", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        starting_client = client_names[0] if client_names else ""

        num_rounds = swarm_rounds
        start_round = 0
        if resume is not None:
            # FLARE's swarm workflow runs rounds start_round .. start_round + num_rounds - 1.
            start_round = resume.start_round
            num_rounds = resume.remaining_rounds
            log.training.info(
                f"Resuming job {resume.from_flare_job or resume.from_job_identifier} "
                f"at round {start_round} of {swarm_rounds}: checkpoint from "
                f"{resume.checkpoint.site or 'this site'} ({resume.checkpoint.source}), "
                f"{num_rounds} rounds left."
            )

        time_limits, time_limit_note = swarm_time_limits_for_job(
            resumed=resume is not None, log=log
        )
        controller = SwarmServerController(
            num_rounds=num_rounds,
            start_round=start_round,
            participating_clients=client_names,
            result_clients=client_names,
            starting_client=starting_client,
            private_p2p=private_p2p,
            aggr_clients=client_names,
            train_clients=client_names,
            **time_limits,
        )
        log.training.info(
            "Swarm controller config: "
            f"private_p2p={private_p2p}, starting_client={starting_client}, "
            f"participants={client_names}, "
            f"time_limits={time_limits or 'NVFlare defaults'}{time_limit_note}"
        )
        for server_name in server_names:
            job_definition.to(controller, server_name)
            job_definition.to(persistor, server_name, id="persistor")
            job_definition.to(
                shareable_generator, server_name, id="shareable_generator"
            )
            job_definition.to(aggregator, server_name, id="aggregator")

        swarm_client_controller = SwarmClientController(
            learn_task_name="train",
            persistor_id="persistor",
            aggregator_id="aggregator",
            shareable_generator_id="shareable_generator",
            min_responses_required=len(client_names),
        )

        for client_name in client_names:
            job_definition.to(
                executor, client_name, tasks=["train", "validate", "submit_model"]
            )
            job_definition.to(
                swarm_client_controller, client_name, tasks=["swarm_*"]
            )
            job_definition.to(persistor, client_name, id="persistor")
            job_definition.to(
                shareable_generator, client_name, id="shareable_generator"
            )
            job_definition.to(aggregator, client_name, id="aggregator")
            job_definition.to(model_selector, client_name, id="model_selector")
            job_definition.to(app_client_custom_dir, client_name)

        generated_jobs_root = os.path.join(job_dir, "generated")
        os.makedirs(generated_jobs_root, exist_ok=True)
        job_definition.export_job(generated_jobs_root)
        job_definition_path = os.path.abspath(
            os.path.join(generated_jobs_root, job_definition.name)
        )
        # The export leaves out limits equal to the Hub's NVFlare defaults.
        write_swarm_time_limits(job_definition_path, time_limits)
        # Every site's copy of the job, and each round-start checkpoint, names the
        # whole run's rounds, so that the run can be resumed at any site.
        write_run_file(
            job_definition_path,
            network_id=network.identifier,
            total_rounds=swarm_rounds,
            start_round=start_round,
            from_flare_job=resume.from_flare_job if resume is not None else "",
        )
        if framework == "pt" and resume_checkpoints_enabled():
            # Round-start checkpoints at every site; only PyTorch runs can resume.
            add_resume_checkpoint_component(job_definition_path)
        if resume is not None:
            shipped = ship_resume_checkpoint(job_definition_path, starting_client, resume)
            log.training.info(
                f"Resume checkpoint added for starting client {starting_client}: {shipped}"
            )
        log.training.info(
            f"Submitting exported job from: {job_definition_path}"
        )

        job_id = session.submit_job(job_definition_path)
        with contextlib.suppress(Exception):
            session.close()

        if not job_id:
            return TrainingJob.objects.create(
                project=project,
                network=network,
                status="FAILED",
                flare_job_id="unknown",
            )

        training_job, _created = ensure_training_job(
            network=network,
            flare_job_id=job_id,
            status="RUNNING",
        )
        return persist_training_job_state(
            training_job,
            flare_job_id=job_id,
            status="RUNNING",
            total_rounds=swarm_rounds,
        )
    except Exception as exc:
        log.training.error(f"Submit job via FLARE API failed: {exc}")
        failed_job = TrainingJob.objects.create(
            project=project,
            network=network,
            status="FAILED",
            flare_job_id="error",
        )
        persist_training_job_state(failed_job, status="FAILED")
        raise


def _network_workspace_root(network: SwarmNetwork) -> Path:
    return (
        Path(settings.BASE_DIR)
        / "workspaces"
        / str(network.project.identifier)
        / str(network.identifier)
        / "workspace"
    )


def _flare_uuid_of(job: TrainingJob) -> str:
    return (
        job.flare_job_uuid
        or extract_flare_job_uuid(job.flare_job_id)
        or str(job.flare_job_id or "")
    )


def list_resume_checkpoints(network: SwarmNetwork, flare_uuid: str) -> list[dict]:
    """Return the checkpoints of a job at this site, newest round first."""
    return [
        checkpoint.as_dict()
        for checkpoint in find_resume_checkpoints(
            _network_workspace_root(network), flare_uuid
        )
    ]


@dataclass
class InterruptedJob:
    """A job that 'swarmed training resume' or 'training checkpoints' is about."""

    network: SwarmNetwork
    flare_uuid: str
    # This Hub's record of the job, if it has one.
    record: TrainingJob | None
    # How the job was found: "record", "job folder", "checkpoint" or
    # "newest checkpoint".
    found_by: str


def _accessible_networks(user):
    """Return the networks of the projects that the user can access."""
    return (
        SwarmNetwork.objects.filter(
            models.Q(project__author=user) | models.Q(project__members=user)
        )
        .select_related("project")
        .distinct()
        .order_by("created_at", "id")
    )


def _accessible_record(user, identifier: str, network) -> TrainingJob | None:
    """Return this Hub's record of a job as the CLI found it before 1.0.6."""
    try:
        record = get_training_job(network=network, identifier=identifier)
    except LookupError:
        return None
    return record if user_can_access_project(user, record.project) else None


def _job_of_checkpoint_path(path: Path) -> str:
    """Return the job in a checkpoint's path, ``<job>/app_<site>/<file>``, or ""."""
    job = path.parent.parent.name
    if path.parent.name.startswith("app_") and FLARE_JOB_ID_RE.fullmatch(job):
        return job
    return ""


def _network_of_checkpoint_path(path: Path) -> str:
    """Return the network of a path in a network's workspace at a Hub, or ""."""
    parts = path.parts
    for index in range(len(parts) - 3):
        if parts[index] == "workspaces" and parts[index + 3] == "workspace":
            if FLARE_JOB_ID_RE.fullmatch(parts[index + 2]):
                return parts[index + 2]
    return ""


def locate_interrupted_job(
    *,
    user,
    identifier: str | None = None,
    network: SwarmNetwork | None = None,
    checkpoint_path: str | None = None,
) -> InterruptedJob:
    """Find the job that 'training resume' or 'training checkpoints' is about.

    The job is found by this Hub's record of it, as before SwarmMed 1.0.6;
    else by its FLARE job ID, or the first 8 or more characters of it, through
    the job's folders in the network's workspace; else by the job that the
    given checkpoint names. Without a job, it is the job of the newest
    round-start checkpoint at this site, unless the network's latest record
    names another job. None of this needs a record, so a site whose job monitor
    never saw the job, such as the standby after a switch, finds it on its disk.

    Args:
        user: The acting user. Only networks of projects the user can access
            are searched.
        identifier: The FLARE job ID, a prefix of it, or a record's identifier.
        network: The network, if one was named or is current.
        checkpoint_path: The checkpoint to resume from, if one was named. It
            must belong to the job and the network.

    Returns:
        InterruptedJob: The network, the FLARE job ID, the record if there is
        one, and how the job was found.

    Raises:
        LookupError: If no job or no network is found, or several match.
        ValueError: If the checkpoint belongs to another job or network.
    """
    checkpoint = Path(checkpoint_path).expanduser() if checkpoint_path else None
    checkpoint_job = checkpoint_network = ""
    if checkpoint is not None:
        metadata = checkpoint_metadata(checkpoint)
        checkpoint_job = metadata.get("job_id") or _job_of_checkpoint_path(checkpoint)
        checkpoint_network = metadata.get("network") or _network_of_checkpoint_path(
            checkpoint.resolve()
        )

    record = None
    if identifier:
        # A record wins, as before.
        record = _accessible_record(user, identifier, network)
        candidate = identifier.strip().lower()
        if record is None and not JOB_ID_PREFIX_RE.fullmatch(candidate):
            raise LookupError(f"Training job '{identifier}' was not found.")
        if record is not None:
            flare_uuid, found_by = _flare_uuid_of(record), "record"
        else:
            flare_uuid, found_by = candidate, "job folder"
    elif checkpoint_job:
        flare_uuid, found_by = checkpoint_job, "checkpoint"
    else:
        if network is None:
            raise LookupError(
                "No current network is selected. Use `swarmed network use "
                "<NETWORK_UUID>` or pass --network."
            )
        require_project_access(user, network.project)
        newest = newest_round_start_job(_network_workspace_root(network))
        latest = get_latest_training_job(network)
        latest_uuid = _flare_uuid_of(latest).lower() if latest else ""
        if not FLARE_JOB_ID_RE.fullmatch(latest_uuid):
            latest_uuid = ""  # a record of a submission that failed names no job
        if newest and latest_uuid and newest != latest_uuid:
            raise LookupError(
                f"Name the job to resume: the latest job of network "
                f"{network.identifier} at this Hub is {latest_uuid}, but the "
                f"newest round-start checkpoint here belongs to job {newest}."
            )
        flare_uuid = newest or latest_uuid
        if not flare_uuid:
            raise LookupError("No training job was found in the current context.")
        found_by = "record" if flare_uuid == latest_uuid else "newest checkpoint"
        record = latest if found_by == "record" else None

    if network is None and record is not None:
        network = record.network
    if network is None and checkpoint_network:
        network = (
            _accessible_networks(user).filter(identifier=checkpoint_network).first()
        )
    if network is None:
        # The one network of the user's projects that holds the job's folders.
        candidates = [
            item
            for item in _accessible_networks(user)
            if job_folders(_network_workspace_root(item), flare_uuid)
        ]
        if len(candidates) > 1:
            raise LookupError(
                f"Job '{identifier or flare_uuid}' has folders in several networks "
                f"here ({', '.join(str(item.identifier) for item in candidates)}); "
                "pass --network."
            )
        if not candidates:
            message = f"Training job '{identifier or flare_uuid}' was not found."
            if checkpoint is not None:
                message += " Pass --network to resume it from the checkpoint."
            raise LookupError(message)
        network = candidates[0]
    require_project_access(user, network.project)

    if record is None and found_by == "job folder":
        names = sorted(
            {
                folder.name
                for folder in job_folders(_network_workspace_root(network), flare_uuid)
            }
        )
        if len(names) > 1:
            raise LookupError(
                f"Several jobs start with '{identifier}' ({', '.join(names)}); "
                "give more of the FLARE job ID."
            )
        if names:
            flare_uuid = names[0]
        elif checkpoint is not None and FLARE_JOB_ID_RE.fullmatch(flare_uuid):
            found_by = "checkpoint"  # no folder of the job here, as with a copy
        else:
            raise LookupError(f"Training job '{identifier}' was not found.")

    if checkpoint_job and checkpoint_job != flare_uuid.lower():
        raise ValueError(
            f"The checkpoint {checkpoint_path} belongs to job {checkpoint_job}, "
            f"not to job {flare_uuid}."
        )
    if checkpoint_network and checkpoint_network != str(network.identifier):
        raise ValueError(
            f"The checkpoint {checkpoint_path} belongs to network "
            f"{checkpoint_network}, not to {network.identifier}."
        )
    if record is None:
        record = TrainingJob.objects.filter(
            network=network, flare_job_uuid=flare_uuid
        ).first()
    return InterruptedJob(
        network=network, flare_uuid=flare_uuid, record=record, found_by=found_by
    )


def resolve_run_total(
    *,
    network: SwarmNetwork,
    flare_uuid: str,
    given: int | None = None,
    checkpoint: ResumeCheckpoint | None = None,
    required: bool = True,
    warnings: list[str] | None = None,
) -> tuple[int | None, str]:
    """Return the number of rounds of the whole run that a job belongs to.

    In this order: ``given`` (``--rounds``); the job's own files at this Hub
    (:func:`training.resume.read_run_plan`); the total that ``checkpoint`` names,
    else the newest checkpoint of the job here that names one. The job's record
    is never used, because releases before 1.0.6 stored a guessed 10 there, and
    neither is the round count of this Hub's training script, which only a new
    job gets.

    Args:
        network: The job's network.
        flare_uuid: The FLARE job ID of the job.
        given: Rounds of the whole run, given by the operator.
        checkpoint: The checkpoint the run is resumed from.
        required: Raise if the total is unknown.
        warnings: Receives a warning if ``given`` differs from a known total.

    Returns:
        tuple[int | None, str]: The total and where it was found ("--rounds",
        one of the job's files or a checkpoint); None and "" if it is unknown
        and not ``required``.

    Raises:
        ValueError: If the total is unknown and ``required``.
    """
    workspace_root = _network_workspace_root(network)
    plan = read_run_plan(workspace_root, flare_uuid)
    known, origin = (plan.total_rounds, plan.origin) if plan else (None, "")
    if known is None:
        candidates = [checkpoint] if checkpoint is not None else []
        candidates.extend(find_resume_checkpoints(workspace_root, flare_uuid))
        for candidate in candidates:
            if candidate.total_rounds:
                known = candidate.total_rounds
                origin = f"the checkpoint {candidate.path}"
                break
    if given:
        if known and given != known and warnings is not None:
            warnings.append(
                f"--rounds {given} differs from the run's {known} rounds in "
                f"{origin}; the resumed run ends after round {given - 1}."
            )
        return int(given), "--rounds"
    if known:
        return known, origin
    if not required:
        return None, ""
    raise ValueError(
        f"The total number of rounds of job {flare_uuid} is not known at this Hub: "
        "neither the job's folders here nor its checkpoint name it, as for a run "
        "started with SwarmMed 1.0.5 or earlier, resumed at a site that did not "
        "run its FL server. Pass --rounds with the number of rounds the run was "
        "started with ('total_rounds' in 'swarmed training list' at the Hub that "
        "started it)."
    )


# FLARE job states in which the job still holds the sites.
_ACTIVE_FLARE_JOB_STATES = ("SUBMITTED", "APPROVED", "DISPATCHED", "RUNNING")


def _short_reason(exc: object, limit: int = 300) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _listed_flare_jobs(reply) -> tuple[list | None, str]:
    """Return the jobs in an FL server's reply to ``list_jobs``.

    NVIDIA FLARE 2.7.1's admin API puts the job list in the reply's meta
    (``meta["jobs"]``, each with ``job_id`` and ``status``, and ``meta["status"]``
    "ok"); the reply's data holds a table for display. A command that failed or
    was refused, or a server that did not answer in time ("Server did not
    respond"), leaves no job list. The reply's own status can be SUCCESS even
    then, for example when the server could not read its job store.

    Returns:
        tuple[list | None, str]: The jobs, or None and what the reply says
        instead.
    """
    if not isinstance(reply, dict):
        return None, _short_reason(repr(reply))
    status = reply.get("status")
    meta = reply.get("meta") if isinstance(reply.get("meta"), dict) else {}
    jobs = meta.get("jobs")
    if isinstance(jobs, list) and meta.get("status") == "ok" and status == "SUCCESS":
        return jobs, ""
    found = (
        status if status != "SUCCESS" else "",
        reply.get("details"),
        meta.get("status") if meta.get("status") != "ok" else "",
        meta.get("info"),
    )
    # FLARE's API status is a str enum; show its value, not its name.
    texts = dict.fromkeys(str(getattr(item, "value", item)) for item in found if item)
    return None, _short_reason("; ".join(texts) or "no reason given")


def _server_jobs(
    network: SwarmNetwork, *, host: str = "", prefix: str = ""
) -> tuple[list | None, str]:
    """Ask an FL server for its jobs (``list_jobs``) in one admin session.

    The admin session gets :func:`flare_connect_timeout` seconds to log in, as
    for a job submission, and the server as long again to answer.

    Args:
        network: The network.
        host: Ask the server at this address and no other. By default the
            network's current server is asked, at the addresses the Hub's admin
            session uses.
        prefix: List only the jobs whose ID starts with this; every job in the
            server's job store by default.

    Returns:
        tuple[list | None, str]: The jobs, each a dict with ``job_id`` and
        ``status``, or None if the server could not be asked; and why it could
        not be asked ("" otherwise).
    """
    admin_target = resolve_admin_session_target(network)
    if not admin_target:
        return None, "this Hub has no admin startup kit for the network"
    admin_username, admin_dir, server_ip = admin_target
    try:
        session = new_secure_session_with_host(
            username=admin_username,
            startup_kit_location=admin_dir,
            host=server_ip,
            timeout=flare_connect_timeout(),
            network_id=network.identifier,
            only_host=host,
        )
    except Exception as exc:
        return None, _short_reason(exc)
    try:
        # FLARE's admin API gives each command 5 s unless told otherwise.
        with contextlib.suppress(Exception):
            session.api.set_command_timeout(flare_connect_timeout())
        reply = session.api.do_command(f"list_jobs {prefix}".strip())
    except Exception as exc:
        return None, f"list_jobs failed: {_short_reason(exc)}"
    finally:
        # The logout keeps the admin API's own 5 s, so that a server that does
        # not answer does not hold the check up for longer.
        with contextlib.suppress(Exception):
            session.api.unset_command_timeout()
        with contextlib.suppress(Exception):
            session.close()
    jobs, problem = _listed_flare_jobs(reply)
    if jobs is None:
        return None, f"list_jobs gave no job list: {problem}"
    return [item for item in jobs if isinstance(item, dict)], ""


def _is_active(item: dict) -> bool:
    return str(item.get("status") or "").upper() in _ACTIVE_FLARE_JOB_STATES


def _job_active_on_server(
    network: SwarmNetwork, job: TrainingJob | str, *, host: str = ""
):
    """Ask an FL server whether it still runs a job.

    Args:
        network: The job's network.
        job: The job, or its FLARE job ID.
        host: Ask the server at this address and no other. By default the
            network's current server is asked, at the addresses the Hub's admin
            session uses.

    Returns:
        tuple[bool | None, str]: True if the server lists the job as active,
        False if it does not, None if the server could not be asked; and why it
        could not be asked ("" otherwise).
    """
    flare_uuid = job if isinstance(job, str) else _flare_uuid_of(job)
    jobs, reason = _server_jobs(network, host=host, prefix=flare_uuid)
    if jobs is None:
        return None, reason
    for item in jobs:
        # list_jobs lists every job whose ID starts with the one given.
        if (
            str(item.get("job_id") or "").lower() == flare_uuid.lower()
            and _is_active(item)
        ):
            return True, ""
    return False, ""


def _check_no_server_runs_job(network: SwarmNetwork, flare_uuid: str) -> list[str]:
    """Refuse to resume a job that an FL server may still run.

    The network's current server is asked first, for all its jobs. It must run
    neither the job nor any other job: one FL server serves one network, so
    another active job is most likely this run, already resumed at another
    site, and a second resume would start a second job at the same sites.

    After a server switch the server this Hub used before is asked as well:
    the standby starts with an empty job store, so only the previous server can
    show that the job still runs there, which would leave the sites split
    between two jobs.

    Returns:
        list[str]: Warnings; one if the previous server could not be asked.

    Raises:
        ValueError: If a server still runs the job, the current server runs
            another job, or the current server cannot be asked.
    """
    jobs, reason = _server_jobs(network)
    if jobs is None:
        raise ValueError(
            f"Could not ask the network's FL server whether job {flare_uuid} is "
            f"still active ({reason}). Resume once the server answers ('swarmed "
            "network status' lists the addresses this Hub uses for it), or pass "
            "--force if you are sure that no FL server still runs the job."
        )
    active = [item for item in jobs if _is_active(item)]
    if any(
        str(item.get("job_id") or "").lower() == flare_uuid.lower() for item in active
    ):
        raise ValueError(
            f"Job {flare_uuid} is still active on the network's FL server. Stop it "
            "first, or pass --force to start a second job next to it. If that "
            "server was restarted after the job was interrupted, its FLARE build "
            "may still list the job as running; then pass --force."
        )
    if active:
        raise ValueError(
            f"The network's FL server already runs job {active[0].get('job_id')} "
            f"({active[0].get('status')}). If that job is this run, resumed at "
            "another site, do not resume it again. Otherwise stop it first at the "
            "Hub that started it, or pass --force."
        )
    previous_host = previous_server_host(network)
    if not previous_host:
        return []
    active, reason = _job_active_on_server(network, flare_uuid, host=previous_host)
    if active:
        raise ValueError(
            f"Job {flare_uuid} is still active on the FL server at {previous_host}, "
            "which this Hub used before switching servers. Stop that server and "
            "keep it down, or pass --force to start a second job next to it."
        )
    if active is None:
        return [
            f"Could not ask the FL server at {previous_host}, which this Hub used "
            f"before switching servers, whether it still runs job {flare_uuid} "
            f"({reason}). Keep that server down: two FL servers must never run for "
            "one network at the same time."
        ]
    return []


def _append_resume_record(network: SwarmNetwork, record: dict) -> None:
    """Keep a per-network list of resumed jobs next to the generated jobs."""
    record_path = (
        Path(settings.BASE_DIR)
        / "workspaces"
        / str(network.project.identifier)
        / str(network.identifier)
        / "job"
        / "resumed_jobs.json"
    )
    try:
        records = json.loads(record_path.read_text()) if record_path.exists() else []
        if not isinstance(records, list):
            records = []
    except (OSError, ValueError):
        records = []
    records.append(record)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(records, indent=2))


def resume_training_job(
    *,
    actor,
    network: SwarmNetwork,
    from_job: TrainingJob | None = None,
    flare_job_uuid: str = "",
    checkpoint_path: str | None = None,
    start_round: int | None = None,
    total_rounds: int | None = None,
    force: bool = False,
    warnings: list[str] | None = None,
) -> TrainingJob:
    """Continue an interrupted swarm run as a new job.

    FLARE cannot continue a swarm job after its server is lost. This submits a
    new job to the network's current server that starts at the interrupted
    round from the newest checkpoint at this site (see :mod:`training.resume`).
    Run it at a site that holds the newest checkpoint; with round-start
    checkpoints every training site does.

    Before it submits, it asks the network's current FL server, and after a
    server switch also the server this Hub used before, whether the job still
    runs there (see :func:`_check_no_server_runs_job`).

    The interrupted job needs no record at this Hub (see
    :func:`locate_interrupted_job`): a Hub without one records it as failed
    once these checks have passed, before it submits the new job.

    Args:
        actor: The user submitting the job.
        network: The network, already switched to a working FL server.
        from_job: This Hub's record of the interrupted job. Without it and
            ``flare_job_uuid``, the network's latest job.
        flare_job_uuid: The FLARE job ID of an interrupted job that this Hub
            has no record of.
        checkpoint_path: A model file to start from instead of the newest one
            found at this site.
        start_round: The round to start at; required when the checkpoint's
            round is unknown.
        total_rounds: Rounds of the whole run; defaults to the total in the
            job's files at this Hub or in its checkpoint (see
            :func:`resolve_run_total`), never to the job's record.
        force: Submit without asking the FL servers whether they still run
            the job.
        warnings: Receives warnings for the caller to show, such as that the
            previous server could not be asked.

    Returns:
        TrainingJob: The new job. The interrupted job is marked FAILED unless it
        has already ended.
    """
    if from_job is None and not flare_job_uuid:
        from_job = get_latest_training_job(network)
        if from_job is None:
            raise LookupError(
                f"Network '{network.name}' has no training job to resume."
            )
    if from_job is not None and from_job.network_id != network.id:
        raise ValueError("The job to resume belongs to another network.")
    flare_uuid = _flare_uuid_of(from_job) if from_job is not None else flare_job_uuid

    if checkpoint_path:
        checkpoint = describe_given_checkpoint(checkpoint_path, start_round)
    else:
        candidates = find_resume_checkpoints(_network_workspace_root(network), flare_uuid)
        if not candidates:
            raise LookupError(
                f"No checkpoint of job {flare_uuid} at this site. Run 'swarmed training "
                f"checkpoints {flare_uuid}' at the other sites and resume from the "
                "site that has the newest one, or copy that file here and pass --checkpoint."
            )
        checkpoint = candidates[0]
        if start_round is not None:
            checkpoint.next_round = start_round
    if checkpoint.job_id and checkpoint.job_id != flare_uuid.lower():
        raise ValueError(
            f"The checkpoint {checkpoint.path} belongs to job {checkpoint.job_id}, "
            f"not to job {flare_uuid}."
        )
    if checkpoint.network and checkpoint.network != str(network.identifier):
        raise ValueError(
            f"The checkpoint {checkpoint.path} belongs to network "
            f"{checkpoint.network}, not to {network.identifier}."
        )
    if checkpoint.next_round is None:
        raise ValueError(
            f"The round that {checkpoint.path} belongs to is unknown; pass --start-round."
        )
    if checkpoint.next_round < 0:
        raise ValueError("The start round cannot be negative.")
    total_warnings: list[str] = []
    total, total_origin = resolve_run_total(
        network=network,
        flare_uuid=flare_uuid,
        given=total_rounds,
        checkpoint=checkpoint,
        warnings=total_warnings,
    )
    if checkpoint.next_round >= total:
        raise ValueError(
            "Nothing to resume: the checkpoint starts at round "
            f"{checkpoint.next_round} and the run has {total} rounds."
        )

    log = get_logger(user=actor, project=network.project)
    for warning in total_warnings:
        log.training.warning(warning)
        if warnings is not None:
            warnings.append(warning)
    if network.status != "RUNNING":
        # Checked here as well, so that no server is asked for a network that
        # this Hub does not run.
        raise ValueError(
            f"Network '{network.name}' is not running. Start the network before "
            "training."
        )
    if not force:
        for warning in _check_no_server_runs_job(network, flare_uuid):
            log.training.warning(warning)
            if warnings is not None:
                warnings.append(warning)
    if from_job is None:
        # Recorded only now, so that a refused resume writes nothing: as failed,
        # the state of an interrupted job, and as the job the new one resumes.
        from_job, created = ensure_training_job(
            network=network, flare_job_id=flare_uuid, status="FAILED"
        )
        if created or from_job.status not in TERMINAL_TRAINING_STATUSES:
            persist_training_job_state(
                from_job,
                status="FAILED",
                total_rounds=total,
                completed_at=timezone.now(),
            )
        if created:
            warning = (
                f"This Hub had no record of job {flare_uuid}; it has recorded the "
                "job as failed."
            )
            log.training.warning(warning)
            if warnings is not None:
                warnings.append(warning)

    plan = ResumePlan(
        checkpoint=checkpoint,
        start_round=int(checkpoint.next_round),
        total_rounds=total,
        from_job_identifier=str(from_job.identifier),
        from_flare_job=flare_uuid,
    )
    new_job = submit_training_job(actor=actor, network=network, resume=plan)

    if new_job.status != "FAILED":
        if from_job.status not in TERMINAL_TRAINING_STATUSES:
            persist_training_job_state(
                from_job, status="FAILED", completed_at=timezone.now()
            )
        log.training.info(
            f"Job {flare_uuid} resumed as {new_job.flare_job_uuid or new_job.flare_job_id} "
            f"from round {plan.start_round} of {plan.total_rounds} "
            f"(total from {total_origin})."
        )
        _append_resume_record(
            network,
            {
                "job": str(new_job.identifier),
                "flare_job": new_job.flare_job_uuid or str(new_job.flare_job_id),
                "from_job": plan.from_job_identifier,
                "from_flare_job": flare_uuid,
                "start_round": plan.start_round,
                "total_rounds": plan.total_rounds,
                "total_rounds_from": total_origin,
                "checkpoint": checkpoint.as_dict(),
                "resumed_at": timezone.now().isoformat(),
            },
        )
    return new_job


def stop_training_job(
    *, actor, network: SwarmNetwork, job: TrainingJob | None = None
) -> TrainingJob:
    """Abort a running training job on a network."""
    job = job or (
        TrainingJob.objects.filter(network=network, status="RUNNING")
        .order_by("-created_at")
        .first()
    )
    if not job:
        raise LookupError("No running job found to stop.")

    log = get_logger(user=actor, project=network.project)
    admin_target = resolve_admin_session_target(network)
    if not admin_target:
        raise LookupError("No admin startup kit found for this center.")

    admin_username, admin_user_dir, server_ip = admin_target
    try:
        session = new_secure_session_with_host(
            username=admin_username,
            startup_kit_location=admin_user_dir,
            host=server_ip,
            network_id=network.identifier,
        )
        job_uuid = job.flare_job_uuid or extract_flare_job_uuid(job.flare_job_id)
        if not job_uuid:
            job_uuid = str(job.flare_job_id)
        session.api.do_command(f"abort_job {job_uuid}")
        return persist_training_job_state(
            job,
            status="STOPPED",
            completed_at=timezone.now(),
            progress_percent=job.progress_percent,
            progress_updated_at=timezone.now(),
        )
    except Exception as exc:
        log.training.error(f"Abort job failed: {exc}")
        err = str(exc).lower()
        if any(token in err for token in ["not running", "invalid job id", "no such job"]):
            return persist_training_job_state(
                job,
                status="COMPLETED",
                completed_at=timezone.now(),
                progress_percent=100,
                progress_updated_at=timezone.now(),
            )
        raise


def get_training_status_payload(
    *, network: SwarmNetwork, job: TrainingJob | None = None
) -> dict:
    """Return a CLI-friendly training status payload.

    Besides the status and progress it gives ``last_round_at`` (ISO 8601, or
    None) and ``stall_warning`` (empty unless the running job has stalled; see
    :mod:`training.stall`).
    """
    job = job or get_latest_training_job(network)
    payload = build_training_status_payload(network, job)
    payload["last_round_at"] = None
    payload["stall_warning"] = ""
    if job:
        normalized_status = str(payload.get("status") or "").upper()
        progress_percent = payload.get("progress")
        progress_updated_at = (
            timezone.now() if progress_percent is not None else None
        )
        if normalized_status in {"COMPLETED", "FAILED", "STOPPED"}:
            persist_training_job_state(
                job,
                status=normalized_status,
                progress_percent=progress_percent,
                completed_at=job.completed_at or timezone.now(),
                progress_updated_at=progress_updated_at,
            )
        elif normalized_status == "RUNNING":
            persist_training_job_state(
                job,
                status="RUNNING",
                progress_percent=progress_percent,
                progress_updated_at=progress_updated_at,
            )
        payload["identifier"] = str(job.identifier)
        payload["network_identifier"] = str(network.identifier)
        payload["project_identifier"] = str(job.project.identifier)
        payload["last_round_at"] = _isoformat(job.last_round_at)
        payload["stall_warning"] = job.stall_warning
    return payload
