"""Utility functions for the training application.

Handles FLARE job identity parsing, runtime log inspection, and communication
with object storage for training code and result uploads.
"""

import ast
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from django.conf import settings

from common.utils import get_s3_client

FLARE_JOB_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

TRAINING_ROUND_PATTERNS = (
    re.compile(r"on round\s+(\d+):.*action=finished_learn_task", re.I),
    re.compile(r"Finished round\s+(\d+)", re.I),
    re.compile(r"Round\s+(\d+)\s+\|", re.I),
    re.compile(r"Round:\s+(\d+)", re.I),
    re.compile(r"finished training round\s+(\d+)", re.I),
    re.compile(r"number of rounds completed\s+(\d+)", re.I),
    re.compile(r"Start aggregation for round\s+(\d+)", re.I),
)

TRAINING_COMPLETION_MARKERS = (
    "ending workflow",
    "child worker process finished",
    "mpm: good bye!",
    "training finished or aborted",
    "swarm learning done",
    "workflow controller finished on all clients",
    "workflow controller done",
    "server runner finished",
)

TRAINING_FAILURE_MARKERS = (
    "execution_exception",
    "fatal_system_error",
    "received failure report from client",
    "data loading error in training script",
    "traceback (most recent call last)",
    "exception ending gatherer",
    "error during model persistence",
)

TRAINING_STOPPED_MARKERS = (
    "abort_job",
    "job aborted",
    "abort signal received",
    "abort requested",
)

TERMINAL_TRAINING_STATUSES = {"COMPLETED", "FAILED", "STOPPED"}

# NVIDIA FLARE 2.7.1 messages about a site that has dropped out of a job. The FL
# server's own log (log.txt of its workspace) names the job and the reason, every
# 10 s while the site's FL client runs without the job (its training process has
# ended), and once when the site's FL client has missed its heartbeats:
#   notified SJ of dead-job: job_id='<job>'; client_name='node-A';
#     reason='missing job on client'   (or reason='client dead')
# The job's server log has, for each notice,
#   [identity=..., run=<job>, wf=...] - received dead job report for client node-A
# and, 60 s after the first, "Client node-A is deemed disconnected!".
DEAD_JOB_NOTICE_RE = re.compile(
    r"notified SJ of dead-job: job_id='(?P<job>[^']*)'; "
    r"client_name='(?P<client>[^']*)'; reason='(?P<reason>[^']*)'"
)
DEAD_JOB_REPORT_RE = re.compile(r"received dead job report for client (?P<client>\S+)")
CLIENT_DISCONNECTED_RE = re.compile(r"Client (?P<client>\S+) is deemed disconnected!")
# Messages that show a site taking part in the job (again).
CLIENT_ACTIVITY_PATTERNS = (
    re.compile(r"updated status of client (?P<client>\S+) on round \d+"),
    re.compile(r"Client (?P<client>\S+) is removed from watch list"),
    re.compile(r"Client (?P<client>\S+) is reconnected"),
)
# Every line matched above contains one of these; other lines are skipped unread.
_CLIENT_STATE_MARKERS = (
    "dead",
    "deemed disconnected",
    "updated status of client",
    "watch list",
    "is reconnected",
)
# The job a FLARE log line names, in its context ("run=<job>") or its message.
_JOB_REFERENCE_RE = re.compile(r"(?:\brun=|job_id=')([0-9a-fA-F-]{36})")
_LOG_LINE_TIME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,(\d{1,6}))?"
)

# Why the FL server counts a site as dropped out, most specific first.
DROPOUT_MISSING = "missing"  # its FL client runs, but the job's process has ended
DROPOUT_DEAD = "dead"  # its FL client has stopped sending heartbeats
DROPOUT_DISCONNECTED = "disconnected"  # the job's server process counts it as gone
DROPOUT_REPORTED = "reported"  # a dead-job report that does not give the reason
_DROPOUT_REASON_RANK = {
    DROPOUT_REPORTED: 0,
    DROPOUT_DISCONNECTED: 1,
    DROPOUT_MISSING: 2,
    DROPOUT_DEAD: 2,
}
_DEAD_JOB_NOTICE_REASONS = {
    "missing job on client": DROPOUT_MISSING,
    "client dead": DROPOUT_DEAD,
}
# FLARE counts a site as alive for 60 s after the first report that its job is
# missing (_dead_client_grace in NVIDIA FLARE 2.7.1): the report can come while
# the site's job process is still starting. A site with no other report counts
# once its reports span as long.
DEAD_CLIENT_GRACE = timedelta(seconds=60)
_REASONS_WITHIN_GRACE = {DROPOUT_MISSING, DROPOUT_REPORTED}


def extract_flare_job_uuid(flare_job_id_raw: str | None) -> str | None:
    """Extract a canonical FLARE job UUID from stored raw job metadata."""
    if not flare_job_id_raw:
        return None

    match = FLARE_JOB_UUID_RE.search(str(flare_job_id_raw))
    if match:
        return match.group(1)

    try:
        parsed = ast.literal_eval(str(flare_job_id_raw))
    except (ValueError, SyntaxError, TypeError):
        return None

    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            data = item.get("data", "")
            if isinstance(data, str):
                match = FLARE_JOB_UUID_RE.search(data)
                if match:
                    return match.group(1)
    return None


def clamp_progress_percent(
    progress_percent: int | None, *, status: str | None = None
) -> int | None:
    """Clamp persisted progress according to runtime status semantics."""
    if progress_percent is None:
        return None

    progress = max(0, int(progress_percent))
    status = str(status or "").upper()
    if status == "COMPLETED":
        return 100
    if status in {"FAILED", "STOPPED"}:
        return min(99, progress)
    return min(99, progress)


def progress_from_rounds(
    rounds_finished: int | None, total_rounds: int | None, *, status: str | None = None
) -> int | None:
    """Calculate progress percent from completed rounds."""
    if total_rounds is None or total_rounds <= 0:
        return None
    rounds_completed = (
        rounds_finished + 1 if rounds_finished is not None and rounds_finished >= 0 else 0
    )
    return clamp_progress_percent(
        int(rounds_completed * 100 / total_rounds), status=status
    )


def extract_training_rounds(log_text: str) -> int:
    """Return the highest completed round found in a log blob."""
    rounds_finished = -1
    for pattern in TRAINING_ROUND_PATTERNS:
        for match in pattern.finditer(log_text):
            round_number = int(match.group(1))
            if round_number > rounds_finished:
                rounds_finished = round_number
    return rounds_finished


def infer_training_terminal_status(log_text: str) -> str | None:
    """Classify a training log tail into a terminal status when possible."""
    lowered = str(log_text or "").lower()
    if not lowered:
        return None

    if any(marker in lowered for marker in TRAINING_FAILURE_MARKERS):
        return "FAILED"
    if any(marker in lowered for marker in TRAINING_STOPPED_MARKERS):
        return "STOPPED"
    if any(marker in lowered for marker in TRAINING_COMPLETION_MARKERS):
        return "COMPLETED"
    return None


def _later(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


def _log_line_time(line: str) -> datetime | None:
    """Return the time at the start of a FLARE log line, if it has one.

    FLARE writes the time in its process's time zone without naming it; its
    containers run in UTC unless their image sets another zone.
    """
    match = _LOG_LINE_TIME_RE.match(line)
    if not match:
        return None
    try:
        when = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if match.group(2):
        when = when.replace(microsecond=int(match.group(2).ljust(6, "0")))
    return when.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ClientDropout:
    """A site that the FL server reports as having dropped out of a job.

    Attributes:
        client: The site's participant name.
        reason: The most specific of the ``DROPOUT_*`` reasons reported.
        first_reported_at: The first report since the site last took part in the
            job, among the log lines read.
        last_reported_at: The latest report.
    """

    client: str
    reason: str
    first_reported_at: datetime
    last_reported_at: datetime


@dataclass
class ClientDropouts:
    """What FLARE's logs of one job say about sites that dropped out of it.

    Attributes:
        reports: Site -> the (time, reason) of each report that it dropped out.
        activity: Site -> the latest time it showed it takes part in the job.
    """

    reports: dict[str, list[tuple[datetime, str]]] = field(default_factory=dict)
    activity: dict[str, datetime] = field(default_factory=dict)

    def add_report(self, client: str, when: datetime, reason: str) -> None:
        """Record a report that a site dropped out."""
        self.reports.setdefault(client, []).append((when, reason))

    def note_activity(self, client: str, when: datetime) -> None:
        """Record that a site showed it takes part in the job."""
        self.activity[client] = _later(self.activity.get(client), when)

    def merge(self, other: "ClientDropouts | None") -> "ClientDropouts":
        """Add what another log of the same job says; returns self."""
        if other is None:
            return self
        for client, reports in other.reports.items():
            self.reports.setdefault(client, []).extend(reports)
        for client, when in other.activity.items():
            self.note_activity(client, when)
        return self

    def dropped(self) -> list[ClientDropout]:
        """Return the sites that are out of the job now, earliest report first.

        A site counts when it has been reported since it last took part in the
        job; a site whose job has only been reported as missing counts once
        those reports span ``DEAD_CLIENT_GRACE``, so that a site whose job is
        still starting is not named. Only the site's own activity ends a
        dropout: its status updates, or its job contacting the FL server again
        ("removed from watch list", "reconnected"). Progress of the other sites
        does not: they can still finish a round to which the site had already
        sent its result, and FLARE reports a dead FL client only once.
        """
        dropouts = []
        for client, reports in self.reports.items():
            active_at = self.activity.get(client)
            current = [
                item for item in reports if active_at is None or item[0] > active_at
            ]
            if not current:
                continue
            first_reported_at = min(when for when, _reason in current)
            last_reported_at = max(when for when, _reason in current)
            if (
                all(reason in _REASONS_WITHIN_GRACE for _when, reason in current)
                and last_reported_at - first_reported_at < DEAD_CLIENT_GRACE
            ):
                continue
            _when, reason = max(
                current, key=lambda item: (_DROPOUT_REASON_RANK[item[1]], item[0])
            )
            dropouts.append(
                ClientDropout(
                    client=client,
                    reason=reason,
                    first_reported_at=first_reported_at,
                    last_reported_at=last_reported_at,
                )
            )
        return sorted(dropouts, key=lambda item: (item.first_reported_at, item.client))


def _concerns_job(line: str, job_uuid: str, job_log: bool) -> bool:
    named = {reference.lower() for reference in _JOB_REFERENCE_RE.findall(line)}
    if named:
        return job_uuid in named
    return job_log


def scan_client_dropouts(
    log_text: str, job_uuid: str | None = None, *, job_log: bool = True
) -> ClientDropouts:
    """Read FLARE's reports of sites that dropped out of a job from a log blob.

    Args:
        log_text: Log lines written by NVIDIA FLARE.
        job_uuid: The job. Lines that name another job are skipped.
        job_log: True for a log of the job itself, whose lines count unless they
            name another job. False for a log of several jobs, such as the FL
            server's own log, of which only lines that name the job count.

    Returns:
        ClientDropouts: The reports and the sites' activity. Lines without a time
        are skipped.
    """
    scan = ClientDropouts()
    wanted = str(job_uuid or "").lower()
    log_text = str(log_text or "")
    for line in log_text.splitlines():
        if not any(marker in line for marker in _CLIENT_STATE_MARKERS):
            continue
        if wanted and not _concerns_job(line, wanted, job_log):
            continue
        when = _log_line_time(line)
        if when is None:
            continue
        if match := DEAD_JOB_NOTICE_RE.search(line):
            reason = _DEAD_JOB_NOTICE_REASONS.get(match["reason"], DROPOUT_REPORTED)
            scan.add_report(match["client"], when, reason)
        elif match := DEAD_JOB_REPORT_RE.search(line):
            scan.add_report(match["client"], when, DROPOUT_REPORTED)
        elif match := CLIENT_DISCONNECTED_RE.search(line):
            scan.add_report(match["client"], when, DROPOUT_DISCONNECTED)
        else:
            for pattern in CLIENT_ACTIVITY_PATTERNS:
                if match := pattern.search(line):
                    scan.note_activity(match["client"], when)
                    break
    return scan


def summarize_training_log(log_text: str, job_uuid: str | None = None) -> dict:
    """Extract progress, terminal state and dropped-out sites from a training log blob.

    Args:
        log_text: The end of one log of the job.
        job_uuid: The job the log belongs to; reports that name another job are
            ignored.

    Returns:
        dict: ``rounds_finished`` (-1 if none), ``terminal_status`` (or None) and
        ``client_dropouts`` (:class:`ClientDropouts`).
    """
    return {
        "rounds_finished": extract_training_rounds(log_text),
        "terminal_status": infer_training_terminal_status(log_text),
        "client_dropouts": scan_client_dropouts(log_text, job_uuid),
    }


def extract_total_rounds_from_flare_config(config: dict, default: int = 10) -> int:
    """Extract the Swarm controller round count from an NVFLARE server config.

    A resumed run starts at ``start_round`` and runs ``num_rounds`` more rounds;
    its round numbers continue the interrupted run, so the count returned is
    ``start_round + num_rounds``.
    """
    plan = extract_swarm_round_plan(config)
    return plan[0] if plan else default


def extract_swarm_round_plan(config: dict) -> tuple[int, int] | None:
    """Return the rounds of the whole run and the start round of a swarm job.

    Reads the swarm controller of an NVFLARE server config. A resumed run starts
    at ``start_round`` and runs ``num_rounds`` more rounds; its round numbers
    continue the interrupted run, so the whole run has ``start_round +
    num_rounds`` rounds.

    Returns:
        tuple[int, int] | None: The whole run's rounds and the start round, or
        None if the config has no swarm controller with a round count.
    """
    workflows = config.get("workflows") if isinstance(config, dict) else None
    for workflow in workflows if isinstance(workflows, list) else []:
        if not isinstance(workflow, dict):
            continue
        args = workflow.get("args") or {}
        if not isinstance(args, dict) or "num_rounds" not in args:
            continue

        workflow_id = str(workflow.get("id") or "").lower()
        workflow_path = str(workflow.get("path") or "").lower()
        is_swarm_controller = (
            workflow_id in {"controller", "swarm_controller"}
            or "swarmservercontroller" in workflow_path
            or "swarm_server_ctl" in workflow_path
        )
        if not is_swarm_controller:
            continue

        try:
            rounds = int(args.get("num_rounds"))
            start_round = int(args.get("start_round") or 0)
        except (TypeError, ValueError):
            continue
        if rounds > 0:
            start_round = max(start_round, 0)
            return rounds + start_round, start_round
    return None


def extract_total_rounds_from_config_file(path: str, default: int = 10) -> int:
    """Read an NVFLARE config file and return its Swarm round count."""
    try:
        with open(path) as handle:
            return extract_total_rounds_from_flare_config(json.load(handle), default)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default


def _scope_logs_to_latest_job(logs: str, job_id_pattern: re.Pattern) -> tuple[str | None, str]:
    """Return the latest job id and only the log segment for that job.

    Runtime containers are reused across jobs. Looking for failure markers in
    the entire Docker log tail can make a previous failed job poison the status
    of a newer running job.
    """
    matches = list(job_id_pattern.finditer(logs or ""))
    if not matches:
        return None, logs

    job_id = matches[-1].group(1)
    segment_start = matches[-1].start()
    for match in reversed(matches[:-1]):
        if match.group(1) == job_id:
            segment_start = match.start()
        else:
            break
    return job_id, logs[segment_start:]


def should_update_terminal_status(
    current_status: str | None, new_status: str | None
) -> bool:
    """Return True when a new terminal status should replace the current one."""
    if not new_status:
        return False

    current_status = str(current_status or "").upper()
    new_status = str(new_status).upper()
    if current_status not in TERMINAL_TRAINING_STATUSES:
        return True
    return current_status == "COMPLETED" and new_status in {
        "FAILED",
        "STOPPED",
    }


def scrape_docker_progress(participant_ids=None):
    """Scrapes progress from local docker containers running NVFlare clients/servers.

    Args:
        participant_ids (list, optional): List of participant IDs to filter containers.
            Defaults to None.

    Returns:
        list: A list of dictionaries with extracted progress info (container, job_id,
            rounds_finished, ended, terminal_status).
    """
    from logs.logger import get_logger
    log = get_logger()
    
    docker_path = shutil.which("docker") or "docker"
    if not docker_path:
        log.training.error("Scrape: Docker CLI not found in PATH")
        return []

    # Get all running container names
    try:
        # Bandit B603: args are a fixed list; shell=False; binary resolved via shutil.which.
        result = subprocess.run(  # nosec B603
            [docker_path, "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, check=True
        )
        container_names = result.stdout.splitlines()
        log.training.debug(f"Scrape: Found running containers: {container_names}")
    except Exception as e:
        log.training.error(f"Scrape: Failed to list docker containers: {e}")
        return []

    # Filter candidates
    candidates = []
    if participant_ids:
        log.training.debug(f"Scrape: Looking for participants: {participant_ids}")
        for p_id in participant_ids:
            # Match exact name or name with prefix/suffix (docker-compose style)
            # Fix: Allow dashes as separators by removing them from the exclusion set
            pattern = re.compile(rf"(^|[^a-zA-Z0-9]){re.escape(p_id)}($|[^a-zA-Z0-9])")
            for c_name in container_names:
                if pattern.search(c_name):
                    candidates.append(c_name)
    else:
        log.training.debug("Scrape: No participant_ids provided, falling back to generic search")
        for c_name in container_names:
            if "client" in c_name.lower() or "server" in c_name.lower() or "nvflare" in c_name.lower():
                candidates.append(c_name)

    log.training.debug(f"Scrape: Filtered candidate containers: {candidates}")

    results = []
    # Match UUIDs (36 chars) after common prefixes
    # Added 'run' variants common in SwarmClientController logs
    job_id_pattern = re.compile(r"(?:Got job|Local Job ID|Deploying job|job_id|job|run|run\s*\(|run[:=])\s*[:=]?\s*([0-9a-f-]{36})", re.I)

    for container in candidates:
        try:
            log.training.debug(f"Scrape: Fetching logs for container: {container}")
            # Get last 2000 lines of logs for more context
            # Bandit B603: args are a fixed list; shell=False; binary resolved via shutil.which.
            log_result = subprocess.run(  # nosec B603
                [docker_path, "logs", "--tail", "2000", container],
                capture_output=True, text=True, check=False
            )
            logs = (log_result.stdout or "") + (log_result.stderr or "")
            if not logs:
                log.training.debug(f"Scrape: No logs found for {container}")
                continue
            
            job_id, scoped_logs = _scope_logs_to_latest_job(logs, job_id_pattern)
            summary = summarize_training_log(scoped_logs)
            rounds_finished = summary["rounds_finished"]
            terminal_status = summary["terminal_status"]
            ended = terminal_status is not None

            if job_id:
                log.training.debug(f"Scrape: Found job_id {job_id} in {container} logs")
                
            if rounds_finished >= 0:
                log.training.debug(f"Scrape: Found rounds_finished {rounds_finished} in {container} logs")
            
            if terminal_status:
                log.training.debug(
                    f"Scrape: Found terminal status '{terminal_status}' in {container} logs"
                )
                
            if job_id or rounds_finished >= 0:
                results.append({
                    "container": container,
                    "job_id": job_id,
                    "rounds_finished": rounds_finished,
                    "ended": ended,
                    "terminal_status": terminal_status,
                })
        except Exception as e:
            log.training.error(f"Scrape: Error processing container {container}: {e}")
            continue
            
    log.training.debug(f"Scrape: Final results: {results}")
    return results


def download_s3_folder(bucket_name, s3_folder, local_dir):
    """Recursively downloads all contents from a specific folder (prefix) in S3 to a local directory.

    This is used to fetch the user's training code.

    Args:
        bucket_name (str): The name of the S3 bucket.
        s3_folder (str): The prefix/folder path in S3.
        local_dir (str): The local path where files will be saved.
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")

    # Iterate through all objects in S3 that start with the given folder
    # prefix.
    for page in paginator.paginate(Bucket=bucket_name, Prefix=s3_folder):
        for obj in page.get("Contents", []):
            # Calculate the relative path from the S3 folder to the file.
            rel_path = os.path.relpath(obj["Key"], s3_folder)
            target = os.path.join(local_dir, rel_path)

            # Ensure the local subdirectory exists.
            if not os.path.exists(os.path.dirname(target)):
                os.makedirs(os.path.dirname(target))

            # If the object is a file (not a directory marker), download it.
            if not obj["Key"].endswith("/"):
                s3.download_file(bucket_name, obj["Key"], target)


def upload_file_to_s3(bucket_name, key, local_path):
    """Uploads a single file to S3.

    The upload requests the same object parameters, including server-side
    encryption, that django-storages applies (AWS_S3_OBJECT_PARAMETERS).

    Args:
        bucket_name (str): The name of the S3 bucket.
        key (str): The key (path) in S3.
        local_path (str): The local path of the file to upload.
    """
    s3 = get_s3_client()
    # Pass a copy: s3transfer adds its own defaults to ExtraArgs in place.
    s3.upload_file(
        local_path,
        bucket_name,
        key,
        ExtraArgs=dict(settings.AWS_S3_OBJECT_PARAMETERS),
    )


def upload_folder_to_s3(bucket_name, local_dir, prefix):
    """Uploads all relevant files from a local directory to S3.

    Specifically used to sync training results (weights, logs) while
    ignoring code and hidden files.

    Args:
        bucket_name (str): Target S3 bucket.
        local_dir (str): Source local directory.
        prefix (str): Target prefix in S3.
    """
    for root, dirs, files in os.walk(local_dir):
        # Modify 'dirs' in-place to skip hidden directories during the walk.
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for filename in files:
            # Security & Efficiency: Skip hidden files and Python source/byte code.
            # We only want to upload data results (CSV, .pt, .npy, etc.)
            if (
                filename.startswith(".")
                or filename.endswith(".py")
                or filename.endswith(".pyc")
            ):
                continue

            full_path = os.path.join(root, filename)
            # Create a relative path to maintain the folder structure in S3.
            rel_path = os.path.relpath(full_path, local_dir)
            s3_key = f"{prefix.rstrip('/')}/{rel_path}"

            upload_file_to_s3(bucket_name, s3_key, full_path)
