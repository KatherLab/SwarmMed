import os
import traceback
import tempfile
from celery import shared_task
from django.utils import timezone
from .models import ResultsVisualizationRun, ResultsVisualizationPlot, TrainingResult
from .visualization import ResultsVisualizationContext
from apps.logs import logger
from apps.logs.context import set_context
from apps.training.models import TrainingJob
from apps.project.models import Project
from django.conf import settings
import boto3
from apps.data.utils import get_s3_client
import ast


@shared_task
def sync_project_results(project_uuid):
    """
    Syncs results from S3 for a given project into the database.
    """
    log = logger.get_logger()
    try:
        project = Project.objects.get(identifier=project_uuid)
        log.results.info(f"Starting results sync for project {project_uuid}")

        s3 = get_s3_client()
        prefix = f"{project.identifier}/results/"
        paginator = s3.get_paginator('list_objects_v2')

        for page in paginator.paginate(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.endswith('/'):
                    continue
                # Expect keys like <project_uuid>/results/<job_id>/<filename>
                parts = key.split('/')
                if len(parts) < 4:
                    log.results.warning(f"Skipping malformed S3 key: {key}")
                    continue
                job_id_from_s3_key = parts[2]
                last_modified = obj.get('LastModified')
                
                job = None
                try:
                    # Find the TrainingJob by its flare_job_id (which might be a complex string)
                    # This logic is adapted from the original views.py
                    job_obj = TrainingJob.objects.filter(project=project, flare_job_id__icontains=job_id_from_s3_key).first()
                    if job_obj:
                        job = job_obj
                    else:
                        # Fallback for older formats or other ways job_id might be stored
                        all_jobs = TrainingJob.objects.filter(project=project)
                        for potential_job in all_jobs:
                            raw_flare_job_id = potential_job.flare_job_id or ""
                            actual_job_id = None
                            try:
                                parsed = ast.literal_eval(raw_flare_job_id)
                                if isinstance(parsed, list):
                                    for it in parsed:
                                        if isinstance(it, dict) and it.get('type') == 'string':
                                            data = it.get('data', '')
                                            if 'Submitted job:' in data:
                                                actual_job_id = data.split(':')[-1].strip()
                                                break
                            except (ValueError, SyntaxError):
                                pass # Not a complex string, try raw
                            
                            if not actual_job_id:
                                actual_job_id = raw_flare_job_id
                            
                            if actual_job_id and actual_job_id.strip() == job_id_from_s3_key:
                                job = potential_job
                                break

                except TrainingJob.DoesNotExist:
                    log.results.warning(f"TrainingJob not found for job_id_from_s3_key: {job_id_from_s3_key} in S3 key: {key}")
                    job = None
                
                if job:
                    tr, created = TrainingResult.objects.get_or_create(
                        job=job,
                        file_path=key,
                        defaults={'file_size': obj.get('Size', 0), 'created_at': last_modified}
                    )
                    if not created:
                        # Update fields if necessary
                        if tr.file_size != obj.get('Size', 0):
                            tr.file_size = obj.get('Size', 0)
                            tr.save(update_fields=['file_size'])
                        if tr.created_at != last_modified:
                            tr.created_at = last_modified
                            tr.save(update_fields=['created_at'])
                else:
                    log.results.warning(f"No associated TrainingJob found for S3 result: {key}. Skipping DB entry.")
        
        log.results.info(f"Finished results sync for project {project_uuid}")

    except Project.DoesNotExist:
        log.results.error(f"Project with ID {project_uuid} not found during sync.")
    except Exception as e:
        log.results.error(f"Error during results sync for project {project_uuid}: {e}", exc_info=True)


@shared_task(bind=True)
def run_results_visualization_task(self, run_id, job_id):
    """Celery task to run a results visualization script."""
    try:
        run = ResultsVisualizationRun.objects.get(id=run_id)
        project = run.project
        user = run.user

        set_context(user=user, project=project)
        log = logger.get_logger()

        log.results.info(f"Starting results visualization for job {job_id}")

        run.status = 'running'
        run.started_at = timezone.now()
        run.celery_task_id = self.request.id
        run.save()

        script_key = f"{project.identifier}/code/results_visualization/visualization.py"
        script_content = ""
        try:
            s3 = boto3.client(
                's3',
                endpoint_url=settings.AWS_S3_ENDPOINT_URL,
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY
            )
            response = s3.get_object(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Key=script_key)
            script_content = response['Body'].read().decode('utf-8')
        except Exception as e:
            log.results.error(f"Could not find or download results visualization script from {script_key}: {e}")
            raise Exception(f"Could not find or download results visualization script from {script_key}")

        with ResultsVisualizationContext(str(project.identifier), job_id, str(run.id)) as context:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as script_file:
                script_with_context = f"""
# Auto-injected results visualization context
import sys
import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import torch
from apps.data.filesystem import DataFileSystem

class ResultsVisualizationContext:
    def __init__(self, context):
        self._context = context

    def save_plot(self, title="Untitled Plot"):
        self._context.save_plot(title)

    def get_model(self, client_name="fl-client-1", model_filename="model.pt"):
        return self._context.get_model(client_name, model_filename)

    def open(self, relative_path, mode='r', **kwargs):
        return self._context.open(relative_path, mode, **kwargs)

    def exists(self, relative_path):
        return self._context.exists(relative_path)

    def listdir(self, relative_path=""):
        return self._context.listdir(relative_path)

    def get_data_path(self, relative_path=""):
        return self._context.get_data_path(relative_path)

visualization = ResultsVisualizationContext(context)

# User script starts here:
{script_content}
"""
                script_file.write(script_with_context)
                script_file.flush()

                try:
                    exec(compile(script_with_context, script_file.name, 'exec'), {'context': context})

                    run.success = True
                    run.output = f"Visualization completed successfully. Generated {len(context.plots)} plots."

                except Exception as e:
                    run.success = False
                    run.error_message = str(e)
                    run.output = traceback.format_exc()
                    log.results.error(f"Results visualization script execution failed: {str(e)}", error=str(e), traceback=traceback.format_exc())

                finally:
                    try:
                        os.unlink(script_file.name)
                    except:
                        log.results.warning(f"Failed to clean up temporary script file: {script_file.name}")

            for plot_data in context.plots:
                ResultsVisualizationPlot.objects.create(
                    visualization_run=run,
                    **plot_data
                )

        run.status = 'completed'
        run.completed_at = timezone.now()
        run.save()

        log.results.info("Results visualization completed successfully")

        return {
            'success': run.success,
            'plots_count': len(context.plots),
            'output': run.output
        }

    except Exception as e:
        try:
            run = ResultsVisualizationRun.objects.get(id=run_id)
            run.status = 'failed'
            run.success = False
            run.error_message = str(e)
            run.output = traceback.format_exc()
            run.completed_at = timezone.now()
            run.save()
            log.results.error(f"Results visualization task failed completely: {str(e)}")
        except:
            pass
        raise e