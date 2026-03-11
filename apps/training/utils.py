"""
Utility functions for the training application.
Handles communication with S3 for downloading training code
and uploading results from the training workspace.
"""

import os
import re
import shutil
import subprocess

from common.utils import get_s3_client


def scrape_docker_progress(participant_ids=None):
    """
    Scrapes progress from local docker containers running NVFlare clients/servers.
    Returns a list of dictionaries with extracted progress info.
    """
    docker_path = shutil.which("docker") or "docker"
    if not docker_path:
        return []

    # Get all running container names
    try:
        # Bandit B603: args are a fixed list; shell=False; binary resolved via shutil.which.
        result = subprocess.run(  # nosec B603
            [docker_path, "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, check=True
        )
        container_names = result.stdout.splitlines()
    except Exception:
        return []

    # Filter candidates
    candidates = []
    if participant_ids:
        for p_id in participant_ids:
            # Match exact name or name with prefix/suffix (docker-compose style)
            # Use a robust regex to avoid false positives (e.g. 'client-1' matching 'client-10')
            pattern = re.compile(rf"(^|[^a-zA-Z0-9-]){re.escape(p_id)}($|[^a-zA-Z0-9-])")
            for c_name in container_names:
                if pattern.search(c_name):
                    candidates.append(c_name)
    else:
        # Generic fallback: look for containers with 'client' or 'server'
        for c_name in container_names:
            if "client" in c_name.lower() or "server" in c_name.lower():
                candidates.append(c_name)

    results = []
    round_patterns = [
        re.compile(r"Finished round\s+(\d+)", re.I),
        re.compile(r"Round\s+(\d+)\s+\|", re.I),
        re.compile(r"Round:\s+(\d+)", re.I),
        re.compile(r"finished training round\s+(\d+)", re.I),
    ]
    # Match UUIDs (36 chars) after common prefixes
    job_id_pattern = re.compile(r"(?:Got job|Local Job ID|Deploying job|job_id|job):\s*([0-9a-f-]{36})", re.I)
    completion_markers = ["ending workflow", "child worker process finished", "MPM: Good Bye!", "training finished", "job finished"]

    for container in candidates:
        try:
            # Get last 1000 lines of logs to be sure we see the round info
            # Bandit B603: args are a fixed list; shell=False; binary resolved via shutil.which.
            log_result = subprocess.run(  # nosec B603
                [docker_path, "logs", "--tail", "1000", container],
                capture_output=True, text=True, check=False
            )
            logs = (log_result.stdout or "") + (log_result.stderr or "")
            if not logs:
                continue
            
            job_id = None
            rounds_finished = -1
            ended = False
            
            # Find Job ID (search from the end)
            job_matches = job_id_pattern.findall(logs)
            if job_matches:
                job_id = job_matches[-1]
                
            # Find Rounds (search from the end)
            for pattern in round_patterns:
                for m in pattern.finditer(logs):
                    rnum = int(m.group(1))
                    if rnum > rounds_finished:
                        rounds_finished = rnum
            
            # Check completion
            if any(m in logs for m in completion_markers):
                ended = True
                
            if job_id or rounds_finished >= 0:
                results.append({
                    "container": container,
                    "job_id": job_id,
                    "rounds_finished": rounds_finished,
                    "ended": ended
                })
        except Exception:
            continue
            
    return results


def download_s3_folder(bucket_name, s3_folder, local_dir):
    """
    Recursively downloads all contents from a specific folder (prefix) in S3
    to a local directory. This is used to fetch the user's training code.

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
    """
    Uploads a single file to S3.
    """
    s3 = get_s3_client()
    s3.upload_file(local_path, bucket_name, key)


def upload_folder_to_s3(bucket_name, local_dir, prefix):
    """
    Uploads all relevant files from a local directory to S3.
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
