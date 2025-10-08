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

logger = logging.getLogger(__name__)

def get_s3_client():
    """
    Create and return an S3 client using settings credentials.
    """
    return boto3.client(
        's3',
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
        endpoint_url=settings.AWS_S3_ENDPOINT_URL
    )

def download_s3_folder(bucket_name, s3_folder, local_dir):
    """    Download the contents of a folder directory in S3.
    """
    s3 = get_s3_client()
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket_name, Prefix=s3_folder):
        for obj in page.get('Contents', []):
            target = os.path.join(local_dir, os.path.relpath(obj['Key'], s3_folder))
            if not os.path.exists(os.path.dirname(target)):
                os.makedirs(os.path.dirname(target))
            if obj['Key'][-1] == '/':
                continue
            s3.download_file(bucket_name, obj['Key'], target)

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
    admin_startup_kit = os.path.join('/app', 'workspaces', str(project.identifier), str(network.identifier), 'workspace', project_name, 'prod_00', 'admin@nvidia.com', 'startup')

    # 2. Create app structure and download files
    os.makedirs(app_custom_dir, exist_ok=True)
    
    try:
        download_s3_folder(settings.AWS_STORAGE_BUCKET_NAME, source_code_prefix, app_custom_dir)
    except ClientError as e:
        logger.error(f"Failed to download training code from S3: {e}")
        # Handle error appropriately

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

    return redirect('network')

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