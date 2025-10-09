from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
import os
import json
import subprocess
import logging
import shutil
import boto3
from botocore.exceptions import ClientError
from django.conf import settings
from apps.network.models import SwarmNetwork, UserCurrentNetwork
from .models import TrainingJob
from django.shortcuts import render
from .utils import download_s3_folder
import time


logger = logging.getLogger(__name__)

@login_required(login_url='/users/signin/')
def start_training(request, network_id):
    """
    Prepares a FLARE app with the user's code and submits it as a job.
    """
    network = SwarmNetwork.objects.get(identifier=network_id)
    project = network.project

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
        logger.error(f"Failed to download training code from S3: {e}")

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

    with open(os.path.join(app_client_custom_dir, 'min_executor.py'), 'w') as f:
        f.write(
            "from nvflare.apis.executor import Executor\n"
            "from nvflare.apis.dxo import DXO, DataKind\n\n"
            "class MinExecutor(Executor):\n"
            "    def init(self):\n"
            "        super().init()\n\n"
            "    def execute(self, task_name, shareable, fl_ctx, abort_signal):\n"
            "        dxo = DXO(data_kind=DataKind.WEIGHTS, data={})\n"
            "        return dxo.to_shareable()\n"
        )

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
        client_names = ['fl-client']

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
    server_cfg = {
        "format_version": 2,
        "workflows": [
            {
                "id": "sg",
                "path": "nvflare.app_common.workflows.scatter_and_gather.ScatterAndGather",
                "args": {
                    "num_rounds": 1,
                    "min_clients": 1,
                    "wait_time_after_min_received": 1
                }
            }
        ]
    }
    client_cfg = {
        "format_version": 2,
        "executors": [
            {
                "id": "executor",
                "path": "custom.min_executor.MinExecutor",
                "args": {},
                "tasks": ["train"]
            }
        ]
    }
    with open(os.path.join(app_server_cfg_dir, 'config_fed_server.json'), 'w') as f:
        json.dump(server_cfg, f, indent=2)
    with open(os.path.join(app_client_cfg_dir, 'config_fed_client.json'), 'w') as f:
        json.dump(client_cfg, f, indent=2)

    logger.info(f"Prepared job at {job_root}")

    # Log files in admin_user_dir
    try:
        logger.info(f"Files in {admin_user_dir}: {os.listdir(admin_user_dir)}")
        with open(os.path.join(admin_user_dir, 'startup', 'fed_admin.json'), 'r') as f:
            logger.info(f"fed_admin.json content: {f.read()}")
    except Exception as e:
        logger.error(f"Could not list files in {admin_user_dir}: {e}")

    # 3. Submit the job using the FLARE API
    try:
        # Add a small delay if needed
        time.sleep(5)

        from nvflare.fuel.flare_api.flare_api import new_secure_session
        
        job_path = os.path.join('/app', job_dir)

        # Open secure session with the admin startup kit (cert auth)
        sess = new_secure_session(username='admin@nvidia.com', startup_kit_location=admin_user_dir)

        # Optional: sanity check connectivity
        # sys_info = sess.get_system_info()

        # Submit job
        rsp = sess.api.do_command(f"submit_job {job_path}") 
        logger.info(f"submit_job reply: {rsp}")
        
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
    except Exception as e:
        logger.error(f"Submit job via FLARE API failed: {e}", exc_info=True)
        TrainingJob.objects.create(
            project=project, 
            network=network, 
            status='FAILED', 
            flare_job_id='exception'
        )

    return redirect('training')

@login_required(login_url='/users/signin/')
def training(request):
    try:
        current_network = UserCurrentNetwork.objects.get(user=request.user).network
    except UserCurrentNetwork.DoesNotExist:
        current_network = None

    context = {
        'segment': 'training',
        'current_network': current_network,
    }
    return render(request, "apps/training.html", context)