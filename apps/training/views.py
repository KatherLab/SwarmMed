from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
import os
import json
import subprocess
from apps.logs.logger import get_logger
import shutil
import boto3
from botocore.exceptions import ClientError
from django.conf import settings
from apps.network.models import SwarmNetwork, UserCurrentNetwork
from .models import TrainingJob
from django.shortcuts import render
from .utils import download_s3_folder, get_s3_client
import time
from django.contrib import messages
from apps.project.models import Project, UserCurrentProject
from apps.results.models import TrainingResult
import tempfile

def get_user_project(request):
    """
    Get the current user's active project identifier.
    
    Args:
        request: Django request object
        
    Returns:
        tuple: (project_uuid, is_valid)
            - project_uuid: String UUID of the project or None
            - is_valid: Boolean indicating if a valid project was found
    """
    try:
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        if not user_current_project.project:
            return None, False
        
        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False


@login_required(login_url='/users/signin/')
def training(request):
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return render(request, "apps/training/no_project_selected.html", {"segment": "training"})
    
    try:
        current_network = UserCurrentNetwork.objects.get(user=request.user).network
        if not current_network or current_network.status != 'RUNNING':
            return render(request, "apps/training/no_network_started.html", {"segment": "training"})
    except UserCurrentNetwork.DoesNotExist:
        return render(request, "apps/training/no_network_started.html", {"segment": "training"})

    # Determine current training job and progress
    is_training_running = False
    training_job = None
    training_progress = 0
    training_status = 'Not started'
    if current_network:
        training_job = TrainingJob.objects.filter(network=current_network).order_by('-created_at').first()
        if training_job:
            training_status = training_job.status.title()
            if training_job.status == 'RUNNING':
                is_training_running = True
                # Derive progress from logs by counting completed rounds vs total rounds (num_rounds in server cfg)
                try:
                    total_rounds = 0
                    server_cfg_path = os.path.join('workspaces', str(training_job.project.identifier), str(current_network.identifier), 'job', 'app_server', 'config', 'config_fed_server.json')
                    if os.path.exists(server_cfg_path):
                        import json as _json
                        with open(server_cfg_path) as _f:
                            _d = _json.load(_f)
                            for wf in _d.get('workflows', []):
                                if wf.get('id') == 'swarm_controller':
                                    total_rounds = int(wf.get('args', {}).get('num_rounds', 0))
                                    break
                    rounds_finished = 0
                    workspace_dir = os.path.join('workspaces', str(training_job.project.identifier), str(current_network.identifier))
                    for root, _, files in os.walk(workspace_dir):
                        for fname in files:
                            if fname.startswith('log_fl') and fname.endswith('.txt'):
                                fpath = os.path.join(root, fname)
                                try:
                                    with open(fpath, 'r') as lf:
                                        for line in lf.readlines():
                                            if 'finished training round' in line:
                                                import re as _re
                                                m = _re.search(r'finished training round (\d+)', line)
                                                if m:
                                                    rnum = int(m.group(1))
                                                    if rnum > rounds_finished:
                                                        rounds_finished = rnum
                                except Exception:
                                    continue
                    if total_rounds > 0:
                        training_progress = min(100, int(rounds_finished * 100 / total_rounds))
                except Exception:
                    training_progress = 0
            elif training_job.status in ['COMPLETED', 'STOPPED', 'FAILED']:
                if training_job.status == 'COMPLETED':
                    training_progress = 100
    context = {
        "segment": "training",
        "current_network": current_network,
        "is_training_running": is_training_running,
        "training_status": training_status,
        "training_progress": training_progress,
    }
    return render(request, "apps/training/training.html", context)


@login_required(login_url='/users/signin/')
def start_training(request, network_id):
    """
    Prepares a FLARE app with the user's code and submits it as a job.
    """
    network = SwarmNetwork.objects.get(identifier=network_id)
    project = network.project
    logger = get_logger(user=request.user, project=project)

    # 1. Define paths
    job_dir = os.path.join('workspaces', str(project.identifier), str(network.identifier), 'job')
    app_custom_dir = os.path.join(job_dir, 'custom')
    source_code_prefix = f"{project.identifier}/code/training/"

    project_name = project.title.replace(' ', '_')
    admin_user_dir = os.path.join('/app', 'workspaces', str(project.identifier), str(network.identifier), 'workspace', project_name, 'prod_00', 'admin@nvidia.com') 

    # 2. Create app structure and download files
    os.makedirs(app_custom_dir, exist_ok=True)
    try:
        download_s3_folder(settings.AWS_STORAGE_BUCKET_NAME, source_code_prefix, app_custom_dir)
    except ClientError as e:
        logger.training.error(f"Failed to download training code from S3: {e}")

    # Build proper NVFLARE job structure:
    job_root = os.path.join('workspaces', str(project.identifier), str(network.identifier), 'job')
    app_server_dir = os.path.join(job_root, 'app_server')
    app_client_dir = os.path.join(job_root, 'app_client')
    app_server_cfg_dir = os.path.join(app_server_dir, 'config')
    app_client_cfg_dir = os.path.join(app_client_dir, 'config')
    app_client_custom_dir = os.path.join(app_client_dir, 'custom')

    os.makedirs(app_server_cfg_dir, exist_ok=True)
    os.makedirs(app_client_cfg_dir, exist_ok=True)
    os.makedirs(app_client_custom_dir, exist_ok=True)

    # Move downloaded code under app_client/custom (so BYOC code is inside the app)
    downloaded_custom_dir = os.path.join(job_root, 'custom')
    if os.path.isdir(downloaded_custom_dir):
        for root, _, files in os.walk(downloaded_custom_dir):
            for f in files:
                src = os.path.join(root, f)
                rel = os.path.relpath(src, downloaded_custom_dir)
                dst = os.path.join(app_client_custom_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
        shutil.rmtree(downloaded_custom_dir, ignore_errors=True)

    # Build meta.json based on current network participants
    if network.participants.filter(role='CLIENT').exists():
        client_names = list(network.participants.filter(role='CLIENT').values_list('participant_id', flat=True))
    else:
        client_names = ['fl-client-1', 'fl-client-2']

    meta = {
        "name": f"{project_name}_job",
        "deploy_map": {
            "app_server": ["server"],
            "app_client": client_names,
        }
    }
    with open(os.path.join(job_root, 'meta.json'), 'w') as f:
        json.dump(meta, f, indent=2)

    # Create placeholder config files if not present.
    server_custom_dir = os.path.join(app_server_dir, 'custom')
    os.makedirs(server_custom_dir, exist_ok=True)

    server_cfg = {
        "format_version": 2,
        "task_data_filters": [],
        "task_result_filters": [],
        "components": [],
        "workflows": [
            {
            "id": "swarm_controller",
            "path": "nvflare.app_common.ccwf.SwarmServerController",
            "args": {
                "num_rounds": 10
            }
            }
        ]
        }
    
    client_cfg = {
        "format_version": 2,
        "executors": [
            {
            "tasks": [
                "train"
            ],
            "executor": {
                "path": "nvflare.app_common.ccwf.comps.np_trainer.NPTrainer",
                "args": {}
            }
            },
            {
            "tasks": ["swarm_*"],
            "executor": {
                "path": "nvflare.app_common.ccwf.SwarmClientController",
                "args": {
                "learn_task_name": "train",
                "learn_task_timeout": 5.0,
                "persistor_id": "persistor",
                "aggregator_id": "aggregator",
                "shareable_generator_id": "shareable_generator",
                "min_responses_required": 2,
                "wait_time_after_min_resps_received": 1
                }
            }
            }
        ],
        "task_result_filters": [],
        "task_data_filters": [],
        "components": [
            {
            "id": "persistor",
            "path": "nvflare.app_common.ccwf.comps.np_file_model_persistor.NPFileModelPersistor",
            "args": {}
            },
            {
            "id": "shareable_generator",
            "name": "FullModelShareableGenerator",
            "args": {}
            },
            {
            "id": "aggregator",
            "name": "InTimeAccumulateWeightedAggregator",
            "args": {
                "expected_data_kind": "WEIGHT_DIFF"
            }
            },
            {
            "id": "model_selector",
            "name": "IntimeModelSelector",
            "args": {}
            }
        ]
        }
    with open(os.path.join(app_server_cfg_dir, 'config_fed_server.json'), 'w') as f:
        json.dump(server_cfg, f, indent=2)
    with open(os.path.join(app_client_cfg_dir, 'config_fed_client.json'), 'w') as f:
        json.dump(client_cfg, f, indent=2)

    logger.training.info(f"Prepared job at {job_root}")

    # Log files in admin_user_dir
    try:
        logger.training.info(f"Files in {admin_user_dir}: {os.listdir(admin_user_dir)}")
        with open(os.path.join(admin_user_dir, 'startup', 'fed_admin.json'), 'r') as f:
            logger.training.info(f"fed_admin.json content: {f.read()}")
    except Exception as e:
        logger.training.error(f"Could not list files in {admin_user_dir}: {e}")

    # 3. Submit the job using the FLARE API
    try:
        # Add a small delay if needed
        time.sleep(5)

        from nvflare.fuel.flare_api.flare_api import new_secure_session
        import socket
        
        job_path = os.path.join('/app', job_dir)

        # Wait for Overseer to be reachable inside the FLARE network
        for _ in range(60):
            try:
                with socket.create_connection(("overseer", 8443), timeout=2):
                    break
            except Exception:
                time.sleep(1)

        # Open secure session with the admin startup kit (cert auth)
        sess = new_secure_session(username='admin@nvidia.com', startup_kit_location=admin_user_dir, timeout=60.0)

        # Optional: sanity check connectivity
        # sys_info = sess.get_system_info()

        # Submit job
        rsp = sess.api.do_command(f"submit_job {job_path}") 
        logger.training.info(f"submit_job reply: {rsp}")
        
        job_id = None
        if isinstance(rsp, dict):
            job_id = rsp.get('job_id') or rsp.get('data') or str(rsp)
        else:
            job_id = getattr(rsp, 'job_id', None) or str(rsp)

        TrainingJob.objects.create(
            project=project,
            network=network,
            status='RUNNING' if job_id else 'FAILED',
            flare_job_id=job_id or 'unknown'
        )
        messages.success(request, f"Successfully submitted job {job_id}")
        
    except Exception as e:
        logger.training.error(f"Submit job via FLARE API failed: {e}", exc_info=True)
        TrainingJob.objects.create(
            project=project, 
            network=network, 
            status='FAILED', 
            flare_job_id='exception'
        )
        messages.error(request, f"Failed to submit job: {e}")

    return redirect('training')

@login_required(login_url='/users/signin/')
def stop_training(request, network_id):
    network = SwarmNetwork.objects.get(identifier=network_id)
    project = network.project
    logger = get_logger(user=request.user, project=project)

    running_job = TrainingJob.objects.filter(network=network, status='RUNNING').order_by('-created_at').first()

    if running_job:
        job_id = running_job.flare_job_id
        project_name = project.title.replace(' ', '_')
        admin_user_dir = os.path.join('/app', 'workspaces', str(project.identifier), str(network.identifier), 'workspace', project_name, 'prod_00', 'admin@nvidia.com')

        try:
            from nvflare.fuel.flare_api.flare_api import new_secure_session
            sess = new_secure_session(username='admin@nvidia.com', startup_kit_location=admin_user_dir)
            rsp = sess.api.do_command(f"abort_job {job_id}")
            logger.training.info(f"Abort job reply: {rsp}")
            running_job.status = 'STOPPED'
            running_job.save()
        except Exception as e:
            logger.training.error(f"Failed to abort job via FLARE API: {e}", exc_info=True)

        # Remove job folder
        job_dir = os.path.join('workspaces', str(project.identifier), str(network.identifier), 'job')
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
            logger.training.info(f"Removed job folder: {job_dir}")

    return redirect('training')
