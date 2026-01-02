"""
Celery tasks for the data app.
Handles background execution of data validation and visualization scripts
to keep the web interface responsive during long-running computations.
"""

import traceback
from celery import shared_task
from django.utils import timezone

from .models import (
    ValidationRun,
    ValidationCheck,
    VisualizationRun,
    VisualizationPlot
)
from .filesystem import DataFileSystem
from .sandbox import run_script_in_sandbox
from apps.logs import logger
from apps.logs.context import set_context


@shared_task(bind=True)
def run_validation_task(self, validation_run_id):
    """
    Background task to execute a user's data validation script.
    """
    try:
        validation_run = ValidationRun.objects.get(id=validation_run_id)
        project = validation_run.project
        user = validation_run.user

        set_context(user=user, project=project)
        log = logger.get_logger()

        log.data.info(f"Starting validation run: {validation_run_id}")

        validation_run.status = 'running'
        validation_run.started_at = timezone.now()
        validation_run.celery_task_id = self.request.id
        validation_run.save()

        if not project.data_validation_script:
            log.data.error("No validation script found for project")
            raise Exception("No validation script found for project")

        # Prepare the data filesystem (downloads files from S3)
        with DataFileSystem(str(project.identifier)) as fs:
            # Synchronize all data files to the local temp directory so they are visible in the sandbox
            fs.download_all()

            script_content = project.data_validation_script.read()
            if isinstance(script_content, bytes):
                script_content = script_content.decode('utf-8')

            # Wrap user script with helper for sandbox
            script_wrapper = f"""
import json
import os

class ValidationHelper:
    def __init__(self, data_root, output_file):
        self.data_root = data_root
        self.output_file = output_file
        self.checks = []

    def add_check(self, name, status, message="", details=None):
        self.checks.append({{
            'name': name,
            'status': status,
            'message': message,
            'details': details or {{}}
        }})
        self._save()

    def get_data_path(self, relative_path=""):
        path = os.path.join(self.data_root, relative_path)
        return path

    def open(self, relative_path, mode='r', **kwargs):
        return open(self.get_data_path(relative_path), mode, **kwargs)

    def exists(self, relative_path):
        return os.path.exists(self.get_data_path(relative_path))

    def listdir(self, relative_path=""):
        return os.listdir(self.get_data_path(relative_path))

    def _save(self):
        with open(self.output_file, 'w') as f:
            json.dump({{'checks': self.checks}}, f)

validation = ValidationHelper('/home/sandboxuser/data', 'results.json')

# --- User script ---
{script_content}
"""

            # Run in sandbox
            result = run_script_in_sandbox(
                script_wrapper,
                fs.temp_dir,
                str(project.identifier),
                run_type="validation"
            )

            validation_run.success = result['success']
            validation_run.output = result['output']

            if not result['success'] and 'error' in result:
                validation_run.error_message = result['error']

            # Save Results
            for check_data in result.get('results', []):
                ValidationCheck.objects.create(
                    validation_run=validation_run,
                    **check_data
                )

        validation_run.status = 'completed'
        validation_run.completed_at = timezone.now()
        validation_run.save()

        return {
            'success': validation_run.success,
            'checks_count': len(result.get('results', [])),
            'output': validation_run.output
        }

    except Exception as e:
        try:
            validation_run = ValidationRun.objects.get(id=validation_run_id)
            validation_run.status = 'failed'
            validation_run.success = False
            validation_run.error_message = str(e)
            validation_run.output = traceback.format_exc()
            validation_run.completed_at = timezone.now()
            validation_run.save()
        except Exception as inner_e:
            log.data.critical(f"Critical failure in run_validation_task error handler: {inner_e}")
        raise e


@shared_task(bind=True)
def run_visualization_task(self, visualization_run_id):
    """
    Background task to execute a user's data visualization script.
    """
    try:
        visualization_run = VisualizationRun.objects.get(id=visualization_run_id)
        project = visualization_run.project
        user = visualization_run.user

        set_context(user=user, project=project)
        log = logger.get_logger()

        log.data.info(f"Starting visualization run: {visualization_run_id}")

        visualization_run.status = 'running'
        visualization_run.started_at = timezone.now()
        visualization_run.celery_task_id = self.request.id
        visualization_run.save()

        if not project.data_visualization_script:
            log.data.error("No visualization script found for project")
            raise Exception("No visualization script found for project")

        with DataFileSystem(str(project.identifier)) as fs:
            # Synchronize all data files to the local temp directory so they are visible in the sandbox
            fs.download_all()

            script_content = project.data_visualization_script.read()
            if isinstance(script_content, bytes):
                script_content = script_content.decode('utf-8')

            # Wrap visualization script with helper for saving plots
            script_wrapper = f"""
import sys
import os
import json
import base64
import io
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

class VisualizationHelper:
    def __init__(self, data_root, plots_dir):
        self.data_root = data_root
        self.plots_dir = plots_dir
        self.plot_count = 0

    def save_plot(self, title="Untitled Plot"):
        if self.plot_count >= 4:
            return
        self.plot_count += 1

        # Save PNG
        png_buf = io.BytesIO()
        plt.savefig(png_buf, format='png', dpi=100, bbox_inches='tight', transparent=True)
        png_data = base64.b64encode(png_buf.getvalue()).decode('utf-8')

        # Save SVG
        svg_buf = io.BytesIO()
        plt.savefig(svg_buf, format='svg', bbox_inches='tight', transparent=True)
        svg_data = base64.b64encode(svg_buf.getvalue()).decode('utf-8')

        plot_data = {{
            'title': title,
            'plot_number': self.plot_count,
            'image_data': png_data,
            'svg_data': svg_data
        }}

        with open(os.path.join(self.plots_dir, f'plot_{{self.plot_count}}.json'), 'w') as f:
            json.dump(plot_data, f)

        plt.clf()

    def get_data_path(self, relative_path=""):
        return os.path.join(self.data_root, relative_path)

    def open(self, relative_path, mode='r', **kwargs):
        return open(self.get_data_path(relative_path), mode, **kwargs)

    def exists(self, relative_path):
        return os.path.exists(self.get_data_path(relative_path))

    def listdir(self, relative_path=""):
        return os.listdir(self.get_data_path(relative_path))

visualization = VisualizationHelper('/home/sandboxuser/data', 'plots')

# --- User script ---
{script_content}
"""

            # Run in sandbox
            result = run_script_in_sandbox(
                script_wrapper,
                fs.temp_dir,
                str(project.identifier),
                run_type="visualization"
            )

            visualization_run.success = result['success']
            visualization_run.output = result['output']

            if not result['success'] and 'error' in result:
                visualization_run.error_message = result['error']

            # Save Plots
            for plot_data in result.get('plots', []):
                VisualizationPlot.objects.create(
                    visualization_run=visualization_run,
                    **plot_data
                )

        visualization_run.status = 'completed'
        visualization_run.completed_at = timezone.now()
        visualization_run.save()

        return {
            'success': visualization_run.success,
            'plots_count': len(result.get('plots', [])),
            'output': visualization_run.output
        }

    except Exception as e:
        try:
            visualization_run = VisualizationRun.objects.get(id=visualization_run_id)
            visualization_run.status = 'failed'
            visualization_run.success = False
            visualization_run.error_message = str(e)
            visualization_run.output = traceback.format_exc()
            visualization_run.completed_at = timezone.now()
            visualization_run.save()
        except Exception as inner_e:
            log.data.critical(f"Critical failure in run_visualization_task error handler: {inner_e}")
        raise e