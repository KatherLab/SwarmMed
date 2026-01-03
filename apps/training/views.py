"""
View functions for the training application.
Handles the training dashboard, job submission via NVIDIA FLARE API,
status polling, and log streaming.
"""

import json
import os
import re
import shutil
import socket
import time
from typing import Optional

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect, render, get_object_or_404
from django.utils import timezone

from apps.logs.logger import get_logger
from apps.network.models import SwarmNetwork, UserCurrentNetwork
from apps.project.models import UserCurrentProject

from .models import TrainingJob
from .utils import download_s3_folder
from ..project.decorators import project_context_required, project_membership_required

logger = get_logger()


def _tail_text(file_path: str, max_bytes: int = 2048 * 1024) -> str:
    """Read up to the last max_bytes of a text file (decoded safely)."""
    try:
        with open(file_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            start = max(0, end - max_bytes)
            f.seek(start)
            data = f.read()
        return data.decode('utf-8', errors='ignore')
    except OSError:
        return ""


def _find_latest_training_log(
    project_id: str,
    network_id: str,
    job_uuid: str,
    cache_ttl_seconds: int = 60,
) -> Optional[str]:
    """Locate the most relevant log file for a given NVFlare job.

    Uses a short cache to avoid repeated directory walks.
    """
    cache_key = f"training_log_path_{project_id}_{network_id}_{job_uuid}"
    cached_path = cache.get(cache_key)
    if cached_path and os.path.exists(cached_path):
        return cached_path

    workspace_root = os.path.join('workspaces', project_id, network_id, 'workspace')
    if not os.path.exists(workspace_root):
        return None

    latest_log = None
    for root, dirs, files in os.walk(workspace_root):
        # Prune obviously irrelevant/hidden directories.
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']

        if job_uuid in root:
            for cand in ('log_fl.txt', 'log.txt'):
                if cand in files:
                    latest_log = os.path.join(root, cand)
                    break
        if latest_log:
            break

    if latest_log and os.path.exists(latest_log):
        cache.set(cache_key, latest_log, cache_ttl_seconds)
        return latest_log

    return None


def get_user_project(request):
    """
    Helper function to retrieve the user's currently active project.
    """
    try:
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        if not user_current_project.project:
            return None, False
        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False


def format_duration(seconds):
    """
    Helper to convert seconds into a human-readable string like '2m 15s'.
    """
    if seconds < 0:
        return "0s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def get_training_progress_info(training_job, current_network):
    """
    Helper function to calculate progress, duration, and ETA for a training job.
    Cached for 30 seconds to reduce filesystem overhead during active training.
    """
    # Cache key based on job ID and status
    cache_key = f'training_progress_{training_job.id}_{training_job.status}'
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        return cached_result
    
    training_progress = 0
    training_status = training_job.status.title()
    is_training_running = False
    duration_str = "-"
    eta_str = "-"

    # Clean the NVFlare job ID.
    job_uuid = str(training_job.flare_job_id)
    match = re.search(r'Submitted job:\s*([0-9a-f-]+)', job_uuid)
    if match:
        job_uuid = match.group(1)
    else:
        match_uuid = re.search(r'([0-9a-f-]{36})', job_uuid)
        if match_uuid:
            job_uuid = match_uuid.group(1)

    if training_job.status == 'RUNNING':
        is_training_running = True

        # Fast path: if Celery recently updated progress, use it.
        have_cached_progress = False
        try:
            if training_job.progress_updated_at:
                age = (timezone.now() - training_job.progress_updated_at).total_seconds()
                if age <= 60 and training_job.progress_percent is not None:
                    training_progress = int(training_job.progress_percent)
                    if training_progress >= 100:
                        training_progress = 99
                    have_cached_progress = True
        except Exception:
            pass

        try:
            # STEP A: Find out how many rounds the user configured.
            total_rounds = 10
            server_cfg_path = os.path.join(
                'workspaces', str(training_job.project.identifier),
                str(current_network.identifier), 'job', 'app_server',
                'config', 'config_fed_server.json'
            )
            if os.path.exists(server_cfg_path):
                with open(server_cfg_path) as f:
                    cfg = json.load(f)
                    for workflow in cfg.get('workflows', []):
                        if workflow.get('id') == 'swarm_controller':
                            total_rounds = int(
                                workflow.get('args', {}).get('num_rounds', 10))
                            break

            # STEP B: Count how many rounds have actually finished.
            rounds_finished = 0
            workspace_dir = os.path.join(
                'workspaces', str(training_job.project.identifier),
                str(current_network.identifier), 'workspace'
            )
            ended = False

            # If we already have a progress value from Celery, avoid doing expensive log work.
            if have_cached_progress:
                ended = False
                rounds_finished = training_job.rounds_finished or 0
            
            # Prefer a direct log if we can find it; otherwise fall back to scanning.
            preferred_log = _find_latest_training_log(
                str(training_job.project.identifier),
                str(current_network.identifier),
                job_uuid,
                cache_ttl_seconds=60,
            )

            round_re = re.compile(r'finished training round (\d+)')

            def scan_log_tail(fpath: str) -> None:
                nonlocal ended, rounds_finished
                data = _tail_text(fpath)
                if not data:
                    return
                if ('ending workflow' in data and 'swarm_controller' in data) or (
                    'child worker process finished with RC 0' in data
                ):
                    ended = True
                for m in round_re.finditer(data):
                    rnum = int(m.group(1))
                    if rnum > rounds_finished:
                        rounds_finished = rnum

            if not have_cached_progress:
                if preferred_log and os.path.exists(preferred_log):
                    scan_log_tail(preferred_log)
                else:
                    for root, dirs, files in os.walk(workspace_dir):
                        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
                        if job_uuid in root:
                            for fname in files:
                                if fname.startswith('log') and fname.endswith('.txt'):
                                    scan_log_tail(os.path.join(root, fname))
                                    if ended:
                                        break
                        if ended:
                            break

            if ended:
                training_progress = 100
                training_status = 'Completed'
                is_training_running = False
                if training_job.status != 'COMPLETED':
                    training_job.status = 'COMPLETED'
                    training_job.completed_at = timezone.now()
                    training_job.save()
            elif not have_cached_progress and total_rounds > 0:
                training_progress = min(99, int(rounds_finished * 100 / total_rounds))
        except Exception as e:
            logger.training.debug(f"Failed to calculate training progress: {e}")
            training_progress = 0

    elif training_job.status == 'COMPLETED':
        training_progress = 100
        training_status = 'Completed'

    # Time Calculation
    now = timezone.now()
    start_time = training_job.created_at

    if training_job.status == 'RUNNING':
        elapsed = (now - start_time).total_seconds()
        duration_str = format_duration(elapsed)
        if training_progress > 0:
            total_est = elapsed / (training_progress / 100.0)
            remaining = total_est - elapsed
            eta_str = format_duration(remaining)
        else:
            eta_str = "Calculating..."
    elif training_job.completed_at:
        elapsed = (training_job.completed_at - start_time).total_seconds()
        duration_str = format_duration(elapsed)
        eta_str = "Finished"

    result = {
        "status": training_status,
        "progress": training_progress,
        "duration": duration_str,
        "eta": eta_str,
        "is_running": is_training_running
    }
    
    # Cache for 5 seconds (only cache if status is RUNNING for dynamic updates)
    if training_job.status == 'RUNNING':
        cache.set(cache_key, result, 5)
    
    return result


@login_required
@project_context_required
def training(request):
    """
    Main training dashboard view.
    """
    try:
        current_network = UserCurrentNetwork.objects.get(user=request.user).network
        if not current_network or current_network.status != 'RUNNING':
            return render(request, "apps/training/no_network_started.html", {"segment": "training"})
    except UserCurrentNetwork.DoesNotExist:
        return render(request, "apps/training/no_network_started.html", {"segment": "training"})

    is_training_running = False
    training_job = None
    training_progress = 0
    training_status = 'Not started'
    training_logs = []
    duration_str = "-"
    eta_str = "-"

    if current_network:
        training_job = TrainingJob.objects.filter(network=current_network).order_by('-created_at').first()

        if training_job:
            info = get_training_progress_info(training_job, current_network)
            training_status = info["status"]
            training_progress = info["progress"]
            duration_str = info["duration"]
            eta_str = info["eta"]
            is_training_running = info["is_running"]

            # Log Collection
            try:
                job_uuid = str(training_job.flare_job_id)
                match = re.search(r'Submitted job:\s*([0-9a-f-]+)', job_uuid)
                if match:
                    job_uuid = match.group(1)
                else:
                    match_uuid = re.search(r'([0-9a-f-]{36})', job_uuid)
                    if match_uuid:
                        job_uuid = match_uuid.group(1)

                latest_log = _find_latest_training_log(
                    str(training_job.project.identifier),
                    str(current_network.identifier),
                    job_uuid,
                    cache_ttl_seconds=60,
                )

                if latest_log and os.path.exists(latest_log):
                    # Tail-read to avoid loading huge logs into memory.
                    tail = _tail_text(latest_log, max_bytes=256 * 1024)
                    lines = tail.splitlines()[-50:]
                    for line in lines:
                        line = line.strip()
                        if not line: continue
                        ts = ''
                        level = ''
                        msg = line
                        logger_name = ''
                        ts_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', line)
                        if ts_match:
                            ts = ts_match.group(1)
                            remaining = line[len(ts):].strip()
                            dash_parts = remaining.split(' - ')
                            if len(dash_parts) >= 3:
                                logger_name = dash_parts[0].strip(' -')
                                level = dash_parts[1].strip()
                                msg = ' - '.join(dash_parts[2:])
                            else:
                                msg = remaining.strip(' -')
                        training_logs.append({
                            'timestamp': ts,
                            'level': level,
                            'message': msg.strip(),
                            'logger': logger_name
                        })
            except Exception as e:
                logger.training.debug(f"Failed to collect training logs: {e}")

    context = {
        "segment": "training",
        "current_network": current_network,
        "is_training_running": is_training_running,
        "training_status": training_status,
        "training_progress": training_progress,
        "training_logs": training_logs,
        "duration_str": duration_str,
        "eta_str": eta_str,
    }
    return render(request, "apps/training/training.html", context)


@login_required
@project_membership_required
def start_training(request, network_id):
    """
    Submit a Job to NVFlare.
    """
    network = SwarmNetwork.objects.get(identifier=network_id)
    project = network.project
    log = get_logger(user=request.user, project=project)

    job_dir = os.path.join('workspaces', str(project.identifier), str(network.identifier), 'job')
    project_name = project.title.replace(' ', '_')
    admin_user_dir = os.path.join(
        '/app', 'workspaces', str(project.identifier),
        str(network.identifier), 'workspace', project_name,
        'prod_00', 'admin@nvidia.com'
    )

    app_server_dir = os.path.join(job_dir, 'app_server')
    app_client_dir = os.path.join(job_dir, 'app_client')
    app_client_custom_dir = os.path.join(app_client_dir, 'custom')

    os.makedirs(os.path.join(app_server_dir, 'config'), exist_ok=True)
    os.makedirs(os.path.join(app_client_dir, 'config'), exist_ok=True)
    os.makedirs(app_client_custom_dir, exist_ok=True)

    source_code_prefix = f"{project.identifier}/code/training/"
    try:
        download_s3_folder(settings.AWS_STORAGE_BUCKET_NAME, source_code_prefix, app_client_custom_dir)
        log.training.info(f"Downloaded training code from S3: {source_code_prefix}")
    except Exception as e:
        log.training.error(f"Failed to download training code: {e}")

    flare_adapter_src = os.path.join(settings.BASE_DIR, 'apps', 'training', 'flare_adapter.py')
    shutil.copyfile(flare_adapter_src, os.path.join(app_client_custom_dir, 'flare_adapter.py'))

    from apps.data.utils import get_internal_s3_download_url, get_s3_client

    # Build a manifest of project data files using an S3 paginator.
    # This avoids recursive folder listing calls which become very slow for large datasets.
    s3 = get_s3_client()
    root_data_prefix = f"{project.identifier}/data/"
    paginator = s3.get_paginator('list_objects_v2')

    log.training.debug(f"Building data manifest for prefix: {root_data_prefix}")
    data_manifest = {}
    file_count = 0
    for page in paginator.paginate(
        Bucket=settings.AWS_STORAGE_BUCKET_NAME,
        Prefix=root_data_prefix,
    ):
        for obj in page.get('Contents', []):
            file_key = obj.get('Key')
            if not file_key or file_key.endswith('/'):
                continue

            # Skip hidden files/dirs (e.g. .DS_Store, .ipynb_checkpoints)
            rel_path = file_key[len(root_data_prefix):]
            if not rel_path or any(part.startswith('.') for part in rel_path.split('/')):
                continue

            data_manifest[rel_path] = get_internal_s3_download_url(file_key, expires=86400)
            file_count += 1

    log.training.info(f"Data manifest built with {file_count} files.")
    manifest_path = os.path.join(app_client_custom_dir, 'data_manifest.json')
    with open(manifest_path, 'w') as f:
        json.dump(data_manifest, f, indent=2)

    training_py_path = os.path.join(app_client_custom_dir, 'training.py')
    if os.path.exists(training_py_path):
        with open(training_py_path, 'r') as f:
            content = f.read()
        replacement = f'main(project_id="{str(project.identifier)}")'
        content = content.replace('main(project_id="default_project")', replacement)
        content = content.replace("main(project_id='default_project')", replacement)
        with open(training_py_path, 'w') as f:
            f.write(content)

    client_names = list(network.participants.filter(role='CLIENT').values_list('participant_id', flat=True))
    if not client_names: client_names = ['fl-client-1', 'fl-client-2']

    meta = {"name": f"{project_name}_job", "deploy_map": {"app_server": ["server"], "app_client": client_names}}
    with open(os.path.join(job_dir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)

    framework = 'pt'
    if os.path.exists(training_py_path):
        with open(training_py_path, 'r') as f:
            script_text = f.read()
            if 'import tensorflow' in script_text or 'import keras' in script_text: framework = 'tf'
            elif 'import sklearn' in script_text: framework = 'np'

    executor_path = "nvflare.app_opt.pt.in_process_client_api_executor.PTInProcessClientAPIExecutor"
    if framework == 'tf': executor_path = "nvflare.app_opt.tf.in_process_client_api_executor.TFInProcessClientAPIExecutor"
    elif framework == 'np': executor_path = "nvflare.app_common.executors.in_process_client_api_executor.InProcessClientAPIExecutor"

    server_cfg = {"format_version": 2, "workflows": [{"id": "swarm_controller", "path": "nvflare.app_common.ccwf.SwarmServerController", "args": {"num_rounds": 10}}]}
    client_cfg = {"format_version": 2,
                  "executors": [{"tasks": ["train"], "executor": {"path": executor_path, "args": {"task_script_path": "custom/training.py"}}},{"tasks": ["swarm_*"], "executor": {"path": "nvflare.app_common.ccwf.SwarmClientController", "args": {"learn_task_name": "train", "persistor_id": "persistor", "aggregator_id": "aggregator", "shareable_generator_id": "shareable_generator", "min_responses_required": len(client_names)}}}],
                  "components": [{"id": "persistor", "path": "nvflare.app_opt.pt.file_model_persistor.PTFileModelPersistor"},
                                 {"id": "shareable_generator", "name": "FullModelShareableGenerator"},
                                 {"id": "aggregator", "name": "InTimeAccumulateWeightedAggregator", "args": {"expected_data_kind": "WEIGHTS"}}]}

    with open(os.path.join(app_server_dir, 'config', 'config_fed_server.json'), 'w') as f: json.dump(server_cfg, f, indent=2)
    with open(os.path.join(app_client_dir, 'config', 'config_fed_client.json'), 'w') as f: json.dump(client_cfg, f, indent=2)

    try:
        from nvflare.fuel.flare_api.flare_api import new_secure_session
        for _ in range(30):
            try:
                socket.create_connection(("overseer", 8443), timeout=2)
                break
            except OSError: time.sleep(1)

        sess = new_secure_session(username='admin@nvidia.com', startup_kit_location=admin_user_dir)
        job_path_absolute = os.path.join('/app', job_dir)
        response = sess.api.do_command(f"submit_job {job_path_absolute}")

        job_id = None
        if isinstance(response, dict): job_id = response.get('job_id') or response.get('data') or str(response)
        else: job_id = getattr(response, 'job_id', None) or str(response)

        TrainingJob.objects.create(project=project, network=network, status='RUNNING' if job_id else 'FAILED', flare_job_id=job_id or 'unknown')
        messages.success(request, f"Successfully submitted job {job_id}")
    except Exception as e:
        log.training.error(f"Submit job via FLARE API failed: {e}")
        TrainingJob.objects.create(project=project, network=network, status='FAILED', flare_job_id='error')
        messages.error(request, f"Failed to submit job: {e}")

    return redirect('training:training')


@login_required
@project_membership_required
def stop_training(request, network_id):
    """
    Aborts the currently running job via the NVFlare API.
    """
    network = get_object_or_404(SwarmNetwork, identifier=network_id)
    job = TrainingJob.objects.filter(network=network, status='RUNNING').first()

    if not job:
        messages.warning(request, "No running job found to stop.")
        return redirect('training:training')

    try:
        from nvflare.fuel.flare_api.flare_api import new_secure_session
        admin_user_dir = os.path.join(
            'workspaces', str(job.project.identifier),
            str(network.identifier), 'startup', 'admin@nvidia.com'
        )
        sess = new_secure_session(username='admin@nvidia.com', startup_kit_location=admin_user_dir)
        job_uuid = str(job.flare_job_id)
        match = re.search(r'([0-9a-f-]{36})', job_uuid)
        if match: job_uuid = match.group(1)
        sess.api.do_command(f"abort_job {job_uuid}")
        job.status = 'STOPPED'
        job.save()
        messages.success(request, f"Successfully aborted job {job_uuid}")
    except Exception as e:
        logger.training.error(f"Abort job failed: {e}")
        messages.error(request, f"Failed to abort job: {e}")

    return redirect('training:training')


@login_required
@project_context_required
def training_status_api(request):
    """
    AJAX endpoint to poll the current training status.
    """
    try:
        current_network = UserCurrentNetwork.objects.get(user=request.user).network
    except UserCurrentNetwork.DoesNotExist:
        return JsonResponse({"status": "no_network"})

    job = TrainingJob.objects.filter(network=current_network).order_by('-created_at').first()
    if not job: return JsonResponse({"status": "idle"})

    info = get_training_progress_info(job, current_network)
    return JsonResponse({
        "status": info["status"],
        "progress": info["progress"],
        "duration": info["duration"],
        "eta": info["eta"],
        "job_id": job.flare_job_id,
        "timestamp": timezone.now().isoformat()
    })


@login_required
@project_context_required
def training_logs_api(request):
    """
    AJAX endpoint returning the last 100 log lines as a JSON list.
    """
    try:
        current_network = UserCurrentNetwork.objects.get(user=request.user).network
    except UserCurrentNetwork.DoesNotExist:
        return JsonResponse({"logs": []})

    logs = []
    try:
        job = TrainingJob.objects.filter(network=current_network).order_by('-created_at').first()
        if not job: return JsonResponse({"logs": logs})

        job_uuid = str(job.flare_job_id)
        match = re.search(r'Submitted job:\s*([0-9a-f-]+)', job_uuid)
        if match: job_uuid = match.group(1)
        else:
            match_uuid = re.search(r'([0-9a-f-]{36})', job_uuid)
            if match_uuid: job_uuid = match_uuid.group(1)

        # Cache key for this specific job log path
        cache_key = f"training_log_path_{job_uuid}"
        latest_log = cache.get(cache_key)

        # Verify if cached path still exists, else clear it
        if latest_log and not os.path.exists(latest_log):
            latest_log = None
            cache.delete(cache_key)

        # If not cached, find it via os.walk
        if not latest_log:
            workspace_root = os.path.join(
                'workspaces', str(job.project.identifier),
                str(current_network.identifier), 'workspace'
            )
            for root, _, files in os.walk(workspace_root):
                if job_uuid in root:
                    for cand in ('log_fl.txt', 'log.txt'):
                        if cand in files:
                            latest_log = os.path.join(root, cand)
                            # Cache valid path for 60 seconds
                            cache.set(cache_key, latest_log, 60)
                            break
                if latest_log: break

        if latest_log and os.path.exists(latest_log):
            with open(latest_log, 'r') as lf:
                lines = lf.readlines()[-100:]
            for line in lines:
                line = line.strip()
                if not line: continue
                ts, level, msg = "", "INFO", line
                ts_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', line)
                if ts_match:
                    ts = ts_match.group(1)
                    remaining = line[len(ts):].strip(' -')
                    parts = remaining.split(' - ')
                    if len(parts) >= 2:
                        if parts[1] in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
                            level, msg = parts[1], ' - '.join(parts[2:])
                        elif parts[0] in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
                            level, msg = parts[0], ' - '.join(parts[1:])
                logs.append({'timestamp': ts, 'level': level, 'message': msg})
    except Exception as e:
        logger.training.debug(f"Error in training_logs_api: {e}")
    return JsonResponse({"logs": logs})