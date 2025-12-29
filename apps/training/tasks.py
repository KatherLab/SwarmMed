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
    Task to monitor running training jobs, check for completion,
    and upload results to S3.
    """
    log = logger.get_logger()
    running_jobs = TrainingJob.objects.filter(status='RUNNING')
    
    for job in running_jobs:
        try:
            project_id = str(job.project.identifier)
            network_id = str(job.network.identifier)
            flare_job_id_raw = job.flare_job_id
            
            # 1. Parse clean UUID from flare_job_id_raw
            # It looks like: [{'type': 'string', 'data': 'Submitted job: <UUID>'}, ...]
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
                    flare_job_uuid = flare_job_id_raw # Fallback
            except:
                flare_job_uuid = flare_job_id_raw

            log.training.info(f"Monitoring Job {job.identifier}. Parsed Flare UUID: {flare_job_uuid}")
            
            # 2. Define workspace root
            # Based on views.py: workspaces/<project_id>/<network_id>/workspace/<project_name>/prod_00/
            project_name = job.project.title.replace(' ', '_')
            workspace_base = os.path.join('workspaces', project_id, network_id, 'workspace', project_name, 'prod_00')
            
            if not os.path.exists(workspace_base):
                # Fallback if project name differs
                workspace_base = os.path.join('workspaces', project_id, network_id, 'workspace')

            # 3. Check for completion in logs
            ended = False
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
                log.training.info(f"Job {job.identifier} ({flare_job_uuid}) detected as COMPLETED. Starting S3 upload.")
                
                # 4. Find all participant folders containing this job UUID
                # Pattern: .../prod_00/<participant_name>/<flare_job_uuid>
                found_folders = []
                if os.path.exists(workspace_base):
                    for participant in os.listdir(workspace_base):
                        p_path = os.path.join(workspace_base, participant)
                        if os.path.isdir(p_path):
                            job_p_path = os.path.join(p_path, flare_job_uuid)
                            if os.path.exists(job_p_path):
                                found_folders.append((participant, job_p_path))

                if found_folders:
                    for participant, local_path in found_folders:
                        s3_prefix = f"{project_id}/results/{flare_job_uuid}/{participant}"
                        log.training.info(f"Uploading {participant} results to S3 {s3_prefix}")
                        upload_folder_to_s3(settings.AWS_STORAGE_BUCKET_NAME, local_path, s3_prefix)
                    
                    job.status = 'COMPLETED'
                    job.completed_at = timezone.now()
                    job.save()
                    log.training.info(f"Job {job.identifier} successfully synced to S3.")
                else:
                    log.training.warning(f"Could not find local result folders for flare_job_uuid {flare_job_uuid} in {workspace_base}")
                    
        except Exception as e:
            log.training.error(f"Error monitoring training job {job.identifier}: {e}")

