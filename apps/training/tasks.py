"""
Celery background tasks for the training application.
Handles periodic monitoring of active training jobs, detection of completion,
and synchronization of result files (weights/logs) to S3 storage.
"""

import ast
import os

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.logs import logger

from .models import TrainingJob
from .utils import upload_folder_to_s3


@shared_task
def monitor_training_jobs():
    """
    Task to monitor training jobs, check for completion by reading workspace logs,
    and automatically upload finalized results to S3.
    """
    # Import inside the task to avoid circular dependency issues with results
    # model.
    from apps.results.models import TrainingResult

    log = logger.get_logger()

    # We check RUNNING jobs to see if they finished,
    # and COMPLETED jobs to ensure their files were actually synced to S3.
    jobs = TrainingJob.objects.filter(status__in=['RUNNING', 'COMPLETED'])

    for job in jobs:
        try:
            # Skip jobs that are already fully synced to S3 to save resources.
            if (TrainingResult.objects.filter(job=job).exists() and
                    job.status == 'COMPLETED'):
                continue

            project_id = str(job.project.identifier)
            network_id = str(job.network.identifier)
            flare_job_id_raw = job.flare_job_id

            # Step 1: Extract the clean Job UUID from the flare_job_id_raw string.
            # NVFlare often returns a complex object or string like "Submitted
            # job: <UUID>".
            flare_job_uuid = None
            try:
                # Try to parse it if it looks like a Python list/dict (logs
                # format).
                parsed = ast.literal_eval(flare_job_id_raw)
                if isinstance(parsed, list):
                    for item in parsed:
                        if (isinstance(item, dict) and
                                item.get('type') == 'string' and
                                'Submitted job:' in item.get('data', '')):
                            flare_job_uuid = item.get(
                                'data', '').split(':')[-1].strip()
                            break
                if not flare_job_uuid:
                    flare_job_uuid = flare_job_id_raw
            except (ValueError, SyntaxError):
                # If parsing fails, assume it's already a clean string or
                # handles itself.
                flare_job_uuid = flare_job_id_raw

            log.training.info(
                f"Monitoring Job {job.identifier}. Flare UUID: {flare_job_uuid}"
            )

            # Step 2: Locate the NVFlare workspace on the local filesystem.
            # The workspace is structured as:
            # workspaces/<project>/<network>/workspace/
            network_workspace_root = os.path.join(
                'workspaces', project_id, network_id, 'workspace'
            )
            workspace_base = None

            # Search for the 'prod_00' directory which contains the actual job
            # output.
            if os.path.exists(network_workspace_root):
                for root, dirs, _ in os.walk(network_workspace_root):
                    if 'prod_00' in dirs:
                        workspace_base = os.path.join(root, 'prod_00')
                        break

            if not workspace_base:
                log.training.warning(
                    f"Workspace folder not found in {network_workspace_root}")
                continue

            # Step 3: Determine if the job has ended by scanning log files.
            ended = (job.status == 'COMPLETED')
            if not ended:
                for root, _, files in os.walk(workspace_base):
                    if ended:
                        break
                    # We only look at logs in directories belonging to this
                    # specific job.
                    if flare_job_uuid in root:
                        for fname in files:
                            if fname.startswith(
                                    'log') and fname.endswith('.txt'):
                                fpath = os.path.join(root, fname)
                                try:
                                    with open(fpath, 'r') as lf:
                                        content = lf.read()
                                        # Specific log markers indicating
                                        # NVFlare finished.
                                        if ('ending workflow swarm_controller' in content or
                                                'child worker process finished' in content):
                                            ended = True
                                            break
                                except OSError:
                                    continue

            # Step 4: If the job is complete, upload participant results to S3.
            if ended:
                log.training.info(
                    f"Job {job.identifier} ({flare_job_uuid}) is COMPLETED. Syncing..."
                )

                # Each client/server has its own folder under 'prod_00'.
                # Inside those, there's a folder named with the job's UUID.
                found_folders = []
                for participant in os.listdir(workspace_base):
                    p_path = os.path.join(workspace_base, participant)
                    if os.path.isdir(p_path):
                        job_p_path = os.path.join(p_path, flare_job_uuid)
                        if os.path.exists(job_p_path):
                            found_folders.append((participant, job_p_path))

                if found_folders:
                    log.training.info(
                        f"Found {len(found_folders)} result folders for upload."
                    )
                    for participant, local_path in found_folders:
                        # S3 path structure:
                        # <project>/results/<job_uuid>/<client_name>/
                        s3_prefix = f"{project_id}/results/{flare_job_uuid}/{participant}"
                        upload_folder_to_s3(
                            settings.AWS_STORAGE_BUCKET_NAME,
                            local_path,
                            s3_prefix
                        )

                    # Update job status in database to trigger UI updates.
                    job.status = 'COMPLETED'
                    if not job.completed_at:
                        job.completed_at = timezone.now()
                    job.save()
                    log.training.info(
                        f"Job {job.identifier} successfully synced to S3.")
                else:
                    log.training.warning(
                        f"No result folders found for {flare_job_uuid}")

        except Exception as e:
            log.training.error(f"Error monitoring job {job.identifier}: {e}")
