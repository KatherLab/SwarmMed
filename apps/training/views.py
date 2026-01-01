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

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.text import slugify

from apps.logs.logger import get_logger
from apps.network.models import SwarmNetwork, UserCurrentNetwork
from apps.project.models import UserCurrentProject

from .models import TrainingJob
from .utils import download_s3_folder

logger = get_logger()


def get_user_project(request):
    """
    Helper function to retrieve the user's currently active project.
    
    This checks the UserCurrentProject model to see which project the 
    logged-in user has selected in their session.

    Returns:
        (str or None, bool): (Project UUID string, Success flag)
    """
    try:
        # Look up the unique record linking the user to their selected project
        user_current_project = UserCurrentProject.objects.get(
            user=request.user)
        
        # Ensure a project is actually linked to that record
        if not user_current_project.project:
            return None, False
            
        # Return the unique identifier (UUID) as a string
        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        # If the user hasn't selected a project yet, return False
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


@login_required(login_url='/users/signin/')
def training(request):
    """
    Main training dashboard view.
    Displays current job status, progress bars, and real-time logs.
    
    Logic flow:
    1. Verify the user has a project selected.
    2. Verify the user has a running network (infrastructure).
    3. Fetch the latest training job for that network.
    4. If a job is running, parse local log files to calculate progress.
    5. Render the training template with all collected data.
    """
    
    # 1. Validation: Project check
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return render(
            request,
            "apps/training/no_project_selected.html",
            {"segment": "training"}
        )

    # 2. Validation: Network check
    # Training requires a "RUNNING" infrastructure (SwarmNetwork) to execute on.
    try:
        current_network = UserCurrentNetwork.objects.get(
            user=request.user).network
        if not current_network or current_network.status != 'RUNNING':
            return render(
                request,
                "apps/training/no_network_started.html",
                {"segment": "training"}
            )
    except UserCurrentNetwork.DoesNotExist:
        return render(
            request,
            "apps/training/no_network_started.html",
            {"segment": "training"}
        )

    # Initialize default UI states for the template context
    is_training_running = False
    training_job = None
    training_progress = 0
    training_status = 'Not started'
    training_logs = []
    duration_str = "-"
    eta_str = "-"

    # 3. Job Detection
    if current_network:
        # Get the most recently created job for this specific network
        training_job = TrainingJob.objects.filter(
            network=current_network
        ).order_by('-created_at').first()

        if training_job:
            # Format status for display (e.g., "RUNNING" -> "Running")
            training_status = training_job.status.title()

            # Clean the NVFlare job ID. It often comes as "Submitted job: <UUID>"
            job_uuid = str(training_job.flare_job_id)
            match = re.search(r'Submitted job:\s*([0-9a-f-]+)', job_uuid)
            if match:
                job_uuid = match.group(1)

            # 4. Progress Calculation (parsing logs)
            if training_job.status == 'RUNNING':
                is_training_running = True
                try:
                    # STEP A: Find out how many rounds the user configured.
                    # We look into the job's server config file.
                    total_rounds = 10  # Default fallback
                    server_cfg_path = os.path.join(
                        'workspaces', str(training_job.project.identifier),
                        str(current_network.identifier), 'job', 'app_server',
                        'config', 'config_fed_server.json'
                    )
                    if os.path.exists(server_cfg_path):
                        with open(server_cfg_path) as f:
                            cfg = json.load(f)
                            # Find the swarm workflow args to get 'num_rounds'
                            for workflow in cfg.get('workflows', []):
                                if workflow.get('id') == 'swarm_controller':
                                    total_rounds = int(
                                        workflow.get(
                                            'args', {}).get(
                                            'num_rounds', 10))
                                    break

                    # STEP B: Count how many rounds have actually finished.
                    # Participant logs contain "finished training round X".
                    rounds_finished = 0
                    workspace_dir = os.path.join(
                        'workspaces', str(training_job.project.identifier),
                        str(current_network.identifier), 'workspace'
                    )
                    ended = False
                    # Walk through the workspace directory to find log files
                    for root, _, files in os.walk(workspace_dir):
                        # Only look in folders belonging to this specific job
                        if job_uuid in root:
                            for fname in files:
                                if fname.startswith('log') and fname.endswith('.txt'):
                                    fpath = os.path.join(root, fname)
                                    try:
                                        with open(fpath, 'r') as lf:
                                            data = lf.read()
                                            # Stricter check for completion markers
                                            if ('ending workflow' in data and 'swarm_controller' in data) or \
                                               ('child worker process finished with RC 0' in data):
                                                ended = True
                                            
                                            # Parse "finished training round (\d+)" to track progress
                                            for m in re.finditer(r'finished training round (\d+)', data):
                                                rnum = int(m.group(1))
                                                if rnum > rounds_finished:
                                                    rounds_finished = rnum
                                    except OSError:
                                        continue

                    # Update status if logs indicate completion
                    if ended:
                        training_progress = 100
                        training_status = 'Completed'
                        is_training_running = False
                        if training_job.status != 'COMPLETED':
                            training_job.status = 'COMPLETED'
                            training_job.completed_at = timezone.now()
                            training_job.save()
                    elif total_rounds > 0:
                        # Cap at 99% until the 'ended' marker is found
                        training_progress = min(99, int(rounds_finished * 100 / total_rounds))
                except Exception as e:
                    logger.training.debug(f"Failed to calculate training progress: {e}")
                    training_progress = 0

            elif training_job.status == 'COMPLETED':
                training_progress = 100

            # 5. Time Calculation
            now = timezone.now()
            start_time = training_job.created_at
            
            if training_job.status == 'RUNNING':
                elapsed = (now - start_time).total_seconds()
                duration_str = format_duration(elapsed)
                
                # Estimate remaining time if some progress exists
                if training_progress > 0:
                    total_est = elapsed / (training_progress / 100.0)
                    remaining = total_est - elapsed
                    eta_str = format_duration(remaining)
                else:
                    eta_str = "Calculating..."
            
            elif training_job.completed_at:
                # Finished job duration
                elapsed = (training_job.completed_at - start_time).total_seconds()
                duration_str = format_duration(elapsed)
                eta_str = "Finished"

            # 6. Log Collection
            # Fetch the last 50 lines of the latest log file for the UI table.
            training_logs = []
            try:
                workspace_root = os.path.join(
                    'workspaces', str(training_job.project.identifier),
                    str(current_network.identifier), 'workspace'
                )
                latest_log = None
                # Locate the specific log file for this job.
                for root, _, files in os.walk(workspace_root):
                    if job_uuid in root:
                        for cand in ['log_fl.txt', 'log.txt']:
                            if cand in files:
                                latest_log = os.path.join(root, cand)
                                break
                    if latest_log:
                        break

                if latest_log and os.path.exists(latest_log):
                    with open(latest_log, 'r') as lf:
                        lines = lf.readlines()[-50:]
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue

                        # Parse NVFlare's standard log format:
                        # YYYY-MM-DD HH:MM:SS,mmm - LOGGER - LEVEL - MESSAGE
                        ts = ''
                        level = ''
                        msg = line
                        logger_name = ''
                        
                        # Extract Timestamp
                        ts_match = re.match(
                            r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})',
                            line
                        )
                        if ts_match:
                            ts = ts_match.group(1)
                            remaining = line[len(ts):].strip()
                            # Split by dashes to find level
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
                training_logs = []

    # Final context for the template
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


@login_required(login_url='/users/signin/')
def start_training(request, network_id):
    """
    Submit a Job to NVFlare.
    
    1. Prepares a Job folder structure inside the project workspace.
    2. Downloads user's training code from S3.
    3. Injects adapter and credentials (.env).
    4. Generates meta.json and framework-specific config files.
    5. Uses NVFlare Admin API to submit the job.
    """
    # Fetch database records
    network = SwarmNetwork.objects.get(identifier=network_id)
    project = network.project
    log = get_logger(user=request.user, project=project)

    # 1. Define paths
    # job_dir: where we build the package to upload
    job_dir = os.path.join(
        'workspaces', str(project.identifier), str(network.identifier), 'job'
    )
    # project_name used for internal NVFlare folder naming
    project_name = project.title.replace(' ', '_')
    # admin_user_dir: location of certificates for NVFlare authentication
    admin_user_dir = os.path.join(
        '/app', 'workspaces', str(project.identifier),
        str(network.identifier), 'workspace', project_name,
        'prod_00', 'admin@nvidia.com'
    )

    # 2. Prepare Folder Structure
    # NVFlare jobs require app_server and app_client folders
    app_server_dir = os.path.join(job_dir, 'app_server')
    app_client_dir = os.path.join(job_dir, 'app_client')
    app_client_custom_dir = os.path.join(app_client_dir, 'custom')

    os.makedirs(os.path.join(app_server_dir, 'config'), exist_ok=True)
    os.makedirs(os.path.join(app_client_dir, 'config'), exist_ok=True)
    os.makedirs(app_client_custom_dir, exist_ok=True)

    # Download training code from S3 bucket into the 'custom' folder
    source_code_prefix = f"{project.identifier}/code/training/"
    try:
        download_s3_folder(
            settings.AWS_STORAGE_BUCKET_NAME,
            source_code_prefix,
            app_client_custom_dir
        )
    except Exception as e:
        log.training.error(f"Failed to download training code: {e}")

    # Inject 'flare_adapter.py' - this is our library that makes training easy
    flare_adapter_src = os.path.join(
        settings.BASE_DIR, 'apps', 'training', 'flare_adapter.py'
    )
    shutil.copyfile(
        flare_adapter_src,
        os.path.join(app_client_custom_dir, 'flare_adapter.py')
    )

    # SECURITY FIX: Generate a data manifest with presigned URLs for each file.
    # This allows workers to download data SECURELY without needing root S3 credentials.
    from apps.data.utils import get_internal_s3_download_url, list_s3_folder
    
    def get_all_files(prefix):
        folders, files = list_s3_folder(prefix)
        all_files = files
        for folder in folders:
            all_files.extend(get_all_files(folder))
        return all_files

    root_data_prefix = f"{project.identifier}/data/"
    project_files = get_all_files(root_data_prefix)
    
    # Manifest maps relative_path -> presigned_url
    data_manifest = {}
    for file_key in project_files:
        rel_path = os.path.relpath(file_key, root_data_prefix)
        # Presign for 24 hours (86400 seconds) - enough for most training jobs
        data_manifest[rel_path] = get_internal_s3_download_url(file_key, expires=86400)

    manifest_path = os.path.join(app_client_custom_dir, 'data_manifest.json')
    with open(manifest_path, 'w') as f:
        json.dump(data_manifest, f, indent=2)

    # SECURITY MITIGATION: Root S3 credentials are NOT injected.
    log.training.info("Generated data_manifest.json with presigned URLs for secure access.")

    # Inject the project UUID into 'training.py' to enable dynamic data paths.
    training_py_path = os.path.join(app_client_custom_dir, 'training.py')
    if os.path.exists(training_py_path):
        with open(training_py_path, 'r') as f:
            content = f.read()

        placeholder = 'main(project_id="default_project")'
        placeholder_2 = "main(project_id='default_project')"
        replacement = f'main(project_id="{str(project.identifier)}")'

        if placeholder in content:
            content = content.replace(placeholder, replacement)
        elif placeholder_2 in content:
            content = content.replace(placeholder_2, replacement)
            
        with open(training_py_path, 'w') as f:
            f.write(content)
        log.training.info("Injected project_id into training.py")

    # 3. Create 'meta.json'
    # This tells NVFlare which app goes to which participant.
    client_names = list(network.participants.filter(
        role='CLIENT'
    ).values_list('participant_id', flat=True))

    if not client_names:
        # Fallback for local development/testing
        client_names = ['fl-client-1', 'fl-client-2']

    meta = {
        "name": f"{project_name}_job",
        "deploy_map": {
            "app_server": ["server"],
            "app_client": client_names,
        }
    }
    with open(os.path.join(job_dir, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)

    # 4. Framework Detection & Config Generation
    framework = 'pt' # Default to PyTorch
    if os.path.exists(training_py_path):
        with open(training_py_path, 'r') as f:
            script_text = f.read()
            if 'import tensorflow' in script_text or 'import keras' in script_text:
                framework = 'tf'
            elif 'import sklearn' in script_text:
                framework = 'np'

    # Select the correct NVFlare executor based on the detected framework
    if framework == 'tf':
        executor_path = "nvflare.app_opt.tf.in_process_client_api_executor.TFInProcessClientAPIExecutor"
    elif framework == 'np':
        executor_path = "nvflare.app_common.executors.in_process_client_api_executor.InProcessClientAPIExecutor"
    else:
        executor_path = "nvflare.app_opt.pt.in_process_client_api_executor.PTInProcessClientAPIExecutor"

    # Server-side workflow config (defines the Swarm controller)
    server_cfg = {
        "format_version": 2,
        "workflows": [{
            "id": "swarm_controller",
            "path": "nvflare.app_common.ccwf.SwarmServerController",
            "args": {"num_rounds": 10}
        }]
    }

    # Client-side component config
    client_cfg = {"format_version": 2,
                  "executors": [{"tasks": ["train"],
                                 "executor": {"path": executor_path,
                                "args": {"task_script_path": "custom/training.py"}}}, 
                                {"tasks": ["swarm_*"],
                                 "executor": {"path": "nvflare.app_common.ccwf.SwarmClientController",
                                              "args": {"learn_task_name": "train",
                                                       "persistor_id": "persistor",
                                                       "aggregator_id": "aggregator",
                                                       "shareable_generator_id": "shareable_generator",
                                                       "min_responses_required": len(client_names)}}}],
                  "components": [{"id": "persistor",
                                  "path": "nvflare.app_opt.pt.file_model_persistor.PTFileModelPersistor"},
                                 {"id": "shareable_generator",
                                  "name": "FullModelShareableGenerator"},
                                 {"id": "aggregator",
                                  "name": "InTimeAccumulateWeightedAggregator",
                                  "args": {"expected_data_kind": "WEIGHTS"}}]}

    # Write configs to files
    with open(os.path.join(app_server_dir, 'config', 'config_fed_server.json'), 'w') as f:
        json.dump(server_cfg, f, indent=2)
    with open(os.path.join(app_client_dir, 'config', 'config_fed_client.json'), 'w') as f:
        json.dump(client_cfg, f, indent=2)

    # 5. Job Submission via API
    try:
        from nvflare.fuel.flare_api.flare_api import new_secure_session

        # Ensure the Overseer container is reachable before attempting auth
        for _ in range(30):
            try:
                socket.create_connection(("overseer", 8443), timeout=2)
                break
            except OSError:
                time.sleep(1)

        # Start secure session with NVFlare Admin
        sess = new_secure_session(
            username='admin@nvidia.com',
            startup_kit_location=admin_user_dir
        )

        # Submit the job folder we just built
        job_path_absolute = os.path.join('/app', job_dir)
        response = sess.api.do_command(f"submit_job {job_path_absolute}")

        # Extract Job ID from API response
        job_id = None
        if isinstance(response, dict):
            job_id = response.get('job_id') or response.get(
                'data') or str(response)
        else:
            job_id = getattr(response, 'job_id', None) or str(response)

        # Record the job in the local Django database
        TrainingJob.objects.create(
            project=project,
            network=network,
            status='RUNNING' if job_id else 'FAILED',
            flare_job_id=job_id or 'unknown'
        )
        messages.success(request, f"Successfully submitted job {job_id}")

    except Exception as e:
        log.training.error(f"Submit job via FLARE API failed: {e}")
        TrainingJob.objects.create(
            project=project, network=network,
            status='FAILED', flare_job_id='error'
        )
        messages.error(request, f"Failed to submit job: {e}")

    return redirect('training:training')


@login_required(login_url='/users/signin/')
def stop_training(request, network_id):
    """
    Aborts the currently running job.
    Uses the NVFlare Admin API to send an 'abort_job' signal.
    """
    network = SwarmNetwork.objects.get(identifier=network_id)
    project = network.project
    log = get_logger(user=request.user, project=project)

    # Find the active job for this network
    running_job = TrainingJob.objects.filter(
        network=network, status='RUNNING'
    ).order_by('-created_at').first()

    if running_job:
        job_id = running_job.flare_job_id
        project_name = project.title.replace(' ', '_')
        admin_user_dir = os.path.join(
            '/app', 'workspaces', str(project.identifier),
            str(network.identifier), 'workspace', project_name,
            'prod_00', 'admin@nvidia.com'
        )

        try:
            # Connect to API and issue abort command
            from nvflare.fuel.flare_api.flare_api import new_secure_session
            sess = new_secure_session(
                username='admin@nvidia.com',
                startup_kit_location=admin_user_dir
            )
            sess.api.do_command(f"abort_job {job_id}")
            # Update local DB record
            running_job.status = 'STOPPED'
            running_job.save()
        except Exception as e:
            log.training.error(f"Failed to abort job: {e}")

    return redirect('training:training')


@login_required(login_url='/users/signin/')
def training_status_api(request):
    """
    AJAX endpoint for real-time dashboard updates.
    Returns status string and progress percentage.
    
    This function logic mirrors 'training' view but is optimized 
    to return pure JSON data for JavaScript consumption.
    """
    try:
        current_network = UserCurrentNetwork.objects.get(
            user=request.user).network
    except UserCurrentNetwork.DoesNotExist:
        return JsonResponse({"status": "No network", "progress": 0})

    status = 'Not started'
    progress = 0
    duration_str = "-"
    eta_str = "-"
    
    try:
        job = TrainingJob.objects.filter(
            network=current_network
        ).order_by('-created_at').first()

        if not job:
            return JsonResponse({"status": status, "progress": progress, "duration": duration_str, "eta": eta_str})

        status = job.status.title()
        job_uuid = str(job.flare_job_id)
        match = re.search(r'Submitted job:\s*([0-9a-f-]+)', job_uuid)
        if match:
            job_uuid = match.group(1)

        # 1. Fetch total rounds from config
        total_rounds = 10 
        server_cfg_path = os.path.join(
            'workspaces', str(job.project.identifier),
            str(current_network.identifier), 'job', 'app_server',
            'config', 'config_fed_server.json'
        )
        if os.path.exists(server_cfg_path):
            try:
                with open(server_cfg_path) as f:
                    cfg = json.load(f)
                    for workflow in cfg.get('workflows', []):
                        if workflow.get('id') == 'swarm_controller':
                            total_rounds = int(workflow.get('args', {}).get('num_rounds', 10))
                            break
            except Exception as e:
                logger.training.debug(f"Failed to load server config: {e}")

        # 2. Check logs for progress
        rounds_finished = 0
        ended = False
        workspace_root = os.path.join(
            'workspaces', str(job.project.identifier),
            str(current_network.identifier), 'workspace'
        )

        for root, _, files in os.walk(workspace_root):
            if job_uuid in root:
                for fname in ('log_fl.txt', 'log.txt'):
                    if fname in files:
                        try:
                            with open(os.path.join(root, fname), 'r') as lf:
                                data = lf.read()
                                # Completion check
                                if ('ending workflow' in data and 'swarm_controller' in data) or \
                                   ('child worker process finished with RC 0' in data):
                                    ended = True
                                # Round tracking
                                for m in re.finditer(r'finished training round (\d+)', data):
                                    r = int(m.group(1))
                                    if r > rounds_finished:
                                        rounds_finished = r
                        except OSError:
                            pass

        # 3. Determine final progress percentage
        if ended:
            progress = 100
            status = 'Completed'
            if job.status != 'COMPLETED':
                job.status = 'COMPLETED'
                job.completed_at = timezone.now()
                job.save()
        elif job.status == 'RUNNING':
            status = 'Running'
            if total_rounds > 0:
                progress = min(99, int(rounds_finished * 100 / total_rounds))
        elif job.status == 'COMPLETED':
            progress = 100
            status = 'Completed'

        # 4. Time Calculation
        now = timezone.now()
        start_time = job.created_at
        
        if job.status == 'RUNNING':
            elapsed = (now - start_time).total_seconds()
            duration_str = format_duration(elapsed)
            if progress > 0:
                total_est = elapsed / (progress / 100.0)
                remaining = total_est - elapsed
                eta_str = format_duration(remaining)
            else:
                eta_str = "Calculating..."
        elif job.completed_at:
            elapsed = (job.completed_at - start_time).total_seconds()
            duration_str = format_duration(elapsed)
            eta_str = "Finished"

    except Exception as e:
        logger.training.debug(f"Error in training_status_api: {e}")

    return JsonResponse({
        "status": status, 
        "progress": progress,
        "duration": duration_str,
        "eta": eta_str
    })


@login_required(login_url='/users/signin/')
def training_logs_api(request):
    """
    AJAX endpoint returning the last 100 log lines as a JSON list.
    
    Parses logs into structured objects:
    [{"timestamp": "...", "level": "INFO", "message": "..."}, ...]
    """
    try:
        current_network = UserCurrentNetwork.objects.get(
            user=request.user).network
    except UserCurrentNetwork.DoesNotExist:
        return JsonResponse({"logs": []})

    logs = []
    try:
        job = TrainingJob.objects.filter(
            network=current_network
        ).order_by('-created_at').first()
        if not job:
            return JsonResponse({"logs": logs})

        job_uuid = str(job.flare_job_id)
        match = re.search(r'Submitted job:\s*([0-9a-f-]+)', job_uuid)
        if match:
            job_uuid = match.group(1)

        workspace_root = os.path.join(
            'workspaces', str(job.project.identifier),
            str(current_network.identifier), 'workspace'
        )
        latest_log = None

        # Find the latest log file for this job
        for root, _, files in os.walk(workspace_root):
            if job_uuid in root:
                for cand in ('log_fl.txt', 'log.txt'):
                    if cand in files:
                        latest_log = os.path.join(root, cand)
                        break
                if latest_log:
                    break

        if latest_log and os.path.exists(latest_log):
            with open(latest_log, 'r') as lf:
                lines = lf.readlines()[-100:]

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # Standard NVFlare log format parsing
                ts = ""
                level = "INFO"
                msg = line

                # Regex for YYYY-MM-DD HH:MM:SS,mmm
                ts_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', line)
                if ts_match:
                    ts = ts_match.group(1)
                    remaining = line[len(ts):].strip(' -')
                    
                    parts = remaining.split(' - ')
                    if len(parts) >= 2:
                        # Extract standard severity levels
                        if parts[1] in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
                            level = parts[1]
                            msg = ' - '.join(parts[2:])
                        elif parts[0] in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
                            level = parts[0]
                            msg = ' - '.join(parts[1:])

                logs.append({
                    'timestamp': ts,
                    'level': level,
                    'message': msg
                })

    except Exception as e:
        logger.training.debug(f"Error in training_logs_api: {e}")

    return JsonResponse({"logs": logs})
