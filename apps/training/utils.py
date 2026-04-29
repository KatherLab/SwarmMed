"""Utility functions for the training application.

Handles FLARE job identity parsing, runtime log inspection, and communication
with object storage for training code and result uploads.
"""

import ast
import os
import re
import shutil
import subprocess

from common.utils import get_s3_client

FLARE_JOB_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

TRAINING_ROUND_PATTERNS = (
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


def summarize_training_log(log_text: str) -> dict:
    """Extract progress and terminal state from a training log blob."""
    return {
        "rounds_finished": extract_training_rounds(log_text),
        "terminal_status": infer_training_terminal_status(log_text),
    }


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
            
            job_id = None
            summary = summarize_training_log(logs)
            rounds_finished = summary["rounds_finished"]
            terminal_status = summary["terminal_status"]
            ended = terminal_status is not None
            
            # Find Job ID (search from the end)
            job_matches = job_id_pattern.findall(logs)
            if job_matches:
                job_id = job_matches[-1]
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

    Args:
        bucket_name (str): The name of the S3 bucket.
        key (str): The key (path) in S3.
        local_path (str): The local path of the file to upload.
    """
    s3 = get_s3_client()
    s3.upload_file(local_path, bucket_name, key)


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
