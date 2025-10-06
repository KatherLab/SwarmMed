from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required
import os
import json
import subprocess
import logging
from apps.network.models import SwarmNetwork
from .models import TrainingJob
from django.shortcuts import render

logger = logging.getLogger(__name__)

@login_required(login_url='/users/signin/')
def start_training(request, network_id):
    """
    Prepares a FLARE app with the user's code and submits it as a job.
    """
    network = SwarmNetwork.objects.get(identifier=network_id)
    project = network.project

    if not project.training_code:
        logger.warning(f"Attempted to start training for project {project.identifier} without training code.")
        return redirect('network_detail', network_id=network.identifier)

    # 1. Define paths
    job_dir = os.path.join('workspaces', str(project.identifier), str(network.identifier), 'job')
    app_custom_dir = os.path.join(job_dir, 'custom')
    
    project_name = project.title.replace(' ', '_')
    admin_startup_kit = os.path.join('/app', 'workspaces', str(project.identifier), str(network.identifier), 'workspace', project_name, 'prod_00', 'admin@nvidia.com', 'startup')

    # 2. Create app structure and files
    os.makedirs(app_custom_dir, exist_ok=True)

    with open(os.path.join(app_custom_dir, 'train.py'), 'wb') as f:
        for chunk in project.training_code.chunks():
            f.write(chunk)

    # 3. Submit the job using subprocess from the app container itself
    try:
        # The command needs to be run from the admin startup directory
        command = [
            './fl_admin.sh',
            '-o', 'overseer:8003',
            'submit_job',
            os.path.join('/app', job_dir) # Use absolute path inside container
        ]
        logger.info(f"Submitting FLARE job with command: {' '.join(command)} in {admin_startup_kit}")
        result = subprocess.run(command, cwd=admin_startup_kit, capture_output=True, text=True)

        logger.info(f"FLARE job submission exited with code {result.returncode}")
        logger.info(f"STDOUT: {result.stdout}")
        logger.error(f"STDERR: {result.stderr}")

        job_id = "unknown"
        if result.returncode == 0 and result.stdout:
            job_id = result.stdout.strip().split(' ')[-1]

        # 4. Create a TrainingJob record
        TrainingJob.objects.create(
            project=project,
            network=network,
            status='RUNNING' if result.returncode == 0 else 'FAILED',
            flare_job_id=job_id
        )
    except Exception as e:
        logger.error(f"An exception occurred while submitting the training job: {e}", exc_info=True)
        TrainingJob.objects.create(
            project=project,
            network=network,
            status='FAILED',
            flare_job_id="exception"
        )

    return redirect('network_detail', network_id=network.identifier)

@login_required(login_url='/users/signin/')
def training(request):
  context = {
    'segment': 'training',
  }
  return render(request, "apps/training.html", context)