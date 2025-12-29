import os
import shutil
import json
from celery import shared_task
from django.utils import timezone
from apps.logs import logger
from apps.project.models import Project
from apps.network.models import SwarmNetwork
from .models import TrainingJob
from .utils import upload_folder_to_s3
from django.conf import settings

@shared_task
def monitor_training_jobs():
    """
    Task to monitor training jobs, check for completion,
    and upload results to S3.
    """
    from apps.results.models import TrainingResult
    log = logger.get_logger()
    
    # Check both RUNNING and COMPLETED jobs (in case COMPLETED were never uploaded)
    jobs = TrainingJob.objects.filter(status__in=['RUNNING', 'COMPLETED'])
    
    for job in jobs:
        try:
            # Check if this job already has results in S3 (basic check)
            if TrainingResult.objects.filter(job=job).exists() and job.status == 'COMPLETED':
                continue

            project_id = str(job.project.identifier)
            network_id = str(job.network.identifier)
            flare_job_id_raw = job.flare_job_id
            
            # 1. Parse clean UUID from flare_job_id_raw
            flare_job_uuid = None
            try:
                import ast
                parsed = ast.literal_eval(flare_job_id_raw)
                if isinstance(parsed, list):
                    for it in parsed:
                        if isinstance(it, dict) and it.get('type') == 'string':
                            data = it.get('data', '')
                            if 'Submitted job:' in data:
                                flare_job_uuid = data.split(':')[-1].strip()
                                break
                if not flare_job_uuid:
                    flare_job_uuid = flare_job_id_raw
            except:
                flare_job_uuid = flare_job_id_raw

            log.training.info(f"Monitoring Job {job.identifier}. Parsed Flare UUID: {flare_job_uuid}")
            
            # 2. Robust workspace root detection
            # Instead of guessing the project folder name (Test vs test), we look for any folder containing 'prod_00'
            network_workspace_root = os.path.join('workspaces', project_id, network_id, 'workspace')
            workspace_base = None
            
            if os.path.exists(network_workspace_root):
                for root, dirs, _ in os.walk(network_workspace_root):
                    if 'prod_00' in dirs:
                        workspace_base = os.path.join(root, 'prod_00')
                        break
            
            if not workspace_base:
                log.training.warning(f"Could not find 'prod_00' folder in {network_workspace_root}")
                continue

            # 3. Check for completion in logs if still running
            ended = (job.status == 'COMPLETED')
            if not ended:
                for root, dirs, files in os.walk(workspace_base):
                    if ended: break
                    if flare_job_uuid in root:
                        for fname in files:
                            if fname.startswith('log') and fname.endswith('.txt'):
                                fpath = os.path.join(root, fname)
                                try:
                                    with open(fpath, 'r') as lf:
                                        data = lf.read()
                                        if 'ending workflow swarm_controller' in data or 'child worker process finished with RC 0' in data:
                                            ended = True
                                            break
                                except: continue
            
            if ended:
                log.training.info(f"Job {job.identifier} ({flare_job_uuid}) detected as COMPLETED. Checking for files to upload.")
                
                # 4. Find all participant folders containing this job UUID
                found_folders = []
                for participant in os.listdir(workspace_base):
                    p_path = os.path.join(workspace_base, participant)
                    if os.path.isdir(p_path):
                        job_p_path = os.path.join(p_path, flare_job_uuid)
                        if os.path.exists(job_p_path):
                            found_folders.append((participant, job_p_path))

                if found_folders:
                    log.training.info(f"Uploading {len(found_folders)} participant folders for job {flare_job_uuid}")
                    for participant, local_path in found_folders:
                        s3_prefix = f"{project_id}/results/{flare_job_uuid}/{participant}"
                        upload_folder_to_s3(settings.AWS_STORAGE_BUCKET_NAME, local_path, s3_prefix)
                    
                    job.status = 'COMPLETED'
                    job.completed_at = timezone.now() if not job.completed_at else job.completed_at
                    job.save()
                    log.training.info(f"Job {job.identifier} successfully synced to S3.")
                else:
                    log.training.warning(f"No result folders found for {flare_job_uuid} in {workspace_base}")
                    
        except Exception as e:
            log.training.error(f"Error monitoring training job {job.identifier}: {e}")

