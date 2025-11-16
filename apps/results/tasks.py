import os
import logging
import ast
from celery import shared_task
from django.conf import settings
from django.contrib import messages
from apps.project.models import Project
from apps.training.models import TrainingJob
from apps.data.utils import get_s3_client

logger = logging.getLogger(__name__)

@shared_task
def sync_project_results(project_identifier):
    """
    Celery task to synchronize local workspace artifacts to S3 for a given project.
    """
    try:
        project = Project.objects.get(identifier=project_identifier)
        logger.info(f"Starting sync_project_results task for project {project.identifier}")
    except Project.DoesNotExist:
        logger.error(f"Project with identifier {project_identifier} does not exist.")
        return

    uploaded = 0
    # For each job in this project, try to upload local workspace artifacts to S3
    for job in TrainingJob.objects.filter(project=project):
        logger.info(f"Processing job {job.id} with raw flare_job_id: {job.flare_job_id}")
        
        try:
            # Extract the actual job ID from the string representation of a list
            flare_job_id_str = job.flare_job_id
            actual_job_id = None
            try:
                flare_job_id_list = ast.literal_eval(flare_job_id_str)
                if isinstance(flare_job_id_list, list) and len(flare_job_id_list) > 0:
                    first_item = flare_job_id_list[0]
                    if isinstance(first_item, dict) and 'data' in first_item:
                        data_str = first_item['data']
                        if 'Submitted job:' in data_str:
                            actual_job_id = data_str.split(':')[-1].strip()
                            logger.info(f"Extracted flare_job_id: {actual_job_id}")
            except (ValueError, SyntaxError):
                actual_job_id = flare_job_id_str
                logger.info(f"flare_job_id is already a simple string: {actual_job_id}")

            if not actual_job_id:
                logger.error(f"Could not extract a valid job ID from {flare_job_id_str}")
                continue

            project_name = project.title.replace(' ', '_')
            search_path = os.path.join(
                '/app', 'workspaces', str(project.identifier), str(job.network.identifier),
                'workspace', project_name, 'prod_00'
            )
            logger.info(f"Search path: {search_path}")

            if not os.path.isdir(search_path):
                logger.warning("Search path does not exist.")
                continue

            for root, dirs, files in os.walk(search_path):
                if actual_job_id in dirs:
                    workspace_root = os.path.join(root, actual_job_id)
                    logger.info(f"Found job directory: {workspace_root}")
                    
                    # Determine existing objects once per job prefix
                    s3 = get_s3_client()
                    prefix = f"{project.identifier}/results/{actual_job_id}/"
                    existing = {}
                    try:
                        paginator = s3.get_paginator('list_objects_v2')
                        for page in paginator.paginate(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix):
                            for obj in page.get('Contents', []) :
                                existing[obj['Key']] = obj.get('Size', 0)
                    except Exception as e:
                        logger.warning(f"Could not list existing objects for prefix {prefix}: {e}")

                    # Upload only missing or different files under the job directory
                    for sub_root, sub_dirs, sub_files in os.walk(workspace_root):
                        if 'startup' in sub_dirs:
                            sub_dirs.remove('startup')
                        for f in sub_files:
                            local_path = os.path.join(sub_root, f)
                            rel = os.path.relpath(local_path, workspace_root)
                            key = f"{project.identifier}/results/{actual_job_id}/{rel}"
                            try:
                                local_size = os.path.getsize(local_path)
                                if existing.get(key) == local_size:
                                    logger.info(f"Skipping existing file with same size: s3://{settings.AWS_STORAGE_BUCKET_NAME}/{key}")
                                    continue
                                s3.upload_file(local_path, settings.AWS_STORAGE_BUCKET_NAME, key)
                                uploaded += 1
                                existing[key] = local_size
                                logger.info(f"Uploaded file: s3://{settings.AWS_STORAGE_BUCKET_NAME}/{key}")
                            except Exception as e:
                                logger.error(f"Error uploading file {local_path}: {e}", exc_info=True)
                                continue
                    # We found the job directory for this job, so we can stop searching for it.
                    break
        except Exception as e:
            logger.error(f"Error during sync for job {job.id}: {e}", exc_info=True)
            continue

    logger.info(f"Sync finished for project {project.identifier}. Uploaded {uploaded} files.")
    return uploaded
