"""
Celery tasks for the data app.
Handles background execution of data validation and visualization scripts
to keep the web interface responsive during long-running computations.
"""

import os
import traceback
import tempfile
from celery import shared_task
from django.utils import timezone

from .models import (
    ValidationRun,
    ValidationCheck,
    VisualizationRun,
    VisualizationPlot
)
from .filesystem import ValidationContext
from .visualization import VisualizationContext
from apps.logs import logger
from apps.logs.context import set_context


@shared_task(bind=True)
def run_validation_task(self, validation_run_id):
    """
    Background task to execute a user's data validation script.

    This task:
    1. Sets up the execution environment and logging.
    2. Downloads and prepares the project data.
    3. Injects a 'validation' helper object into the script.
    4. Executes the script and captures results or errors.
    """
    try:
        # 1. Initialization and Setup
        validation_run = ValidationRun.objects.get(id=validation_run_id)
        project = validation_run.project
        user = validation_run.user

        # Set the logging context so logs are associated with this user/project
        set_context(user=user, project=project)
        log = logger.get_logger()

        log.data.info(f"Starting validation run: {validation_run_id}")

        # Update run status to 'running'
        validation_run.status = 'running'
        validation_run.started_at = timezone.now()
        validation_run.celery_task_id = self.request.id
        validation_run.save()

        # Ensure there is a script to run
        if not project.data_validation_script:
            log.data.error("No validation script found for project")
            raise Exception("No validation script found for project")

        # 2. Execution Environment Preparation
        with ValidationContext(
            str(project.identifier),
            str(validation_run_id)
        ) as context:
            # Read the user's script content
            script_content = project.data_validation_script.read()
            if isinstance(script_content, bytes):
                script_content = script_content.decode('utf-8')

            # Create a temporary local file for the script
            with tempfile.NamedTemporaryFile(
                mode='w',
                suffix='.py',
                delete=False
            ) as script_file:
                # We wrap the user's script with a helper class that provides
                # easy access to the data filesystem and check reporting.
                script_wrapper = f"""
# Auto-injected validation context
import sys
import os

class ValidationHelper:
    def __init__(self, filesystem, checks):
        self.filesystem = filesystem
        self.checks = checks

    def add_check(self, name, status, message="", details=None):
        self.checks.append({{
            'name': name,
            'status': status,
            'message': message,
            'details': details or {{}}
        }})

    def get_data_path(self, relative_path=""):
        return (self.filesystem.get_path(relative_path)
                if relative_path else self.filesystem.temp_dir)

    def open(self, relative_path, mode='r', **kwargs):
        return self.filesystem.open(relative_path, mode, **kwargs)

    def exists(self, relative_path):
        return self.filesystem.exists(relative_path)

    def listdir(self, relative_path=""):
        return self.filesystem.listdir(relative_path)

# Initialize context (this will be available to the user script as 'validation')
_helper = ValidationHelper(_filesystem, _checks)
validation = _helper

# --- User script starts here ---
{script_content}
"""
                script_file.write(script_wrapper)
                script_file.flush()

                # 3. Script Execution
                try:
                    # Define the global variables available to the script
                    script_globals = {
                        '_filesystem': context.filesystem,
                        '_checks': context.checks,
                        '__file__': script_file.name,
                        '__name__': '__main__'
                    }

                    # Compile and execute the wrapped script
                    exec(
                        compile(script_wrapper, script_file.name, 'exec'),
                        script_globals
                    )

                    # If we reached here, execution finished without unhandled
                    # errors
                    validation_run.success = True
                    validation_run.output = "Validation completed successfully"

                except Exception as e:
                    # Capture any error that happened during script execution
                    validation_run.success = False
                    validation_run.error_message = str(e)
                    validation_run.output = traceback.format_exc()

                    log.data.error(
                        f"Validation script execution failed: {str(e)}"
                    )

                finally:
                    # Clean up the temporary script file
                    try:
                        os.unlink(script_file.name)
                    except Exception:
                        pass

            # 4. Save Results
            # Create a database record for each check reported by the script
            for check_data in context.checks:
                ValidationCheck.objects.create(
                    validation_run=validation_run,
                    **check_data
                )

        # Mark the run as completed
        validation_run.status = 'completed'
        validation_run.completed_at = timezone.now()
        validation_run.save()

        log.data.info("Validation run finished")

        return {
            'success': validation_run.success,
            'checks_count': len(context.checks),
            'output': validation_run.output
        }

    except Exception as e:
        # Handle unexpected errors in the task logic itself
        try:
            validation_run = ValidationRun.objects.get(id=validation_run_id)
            validation_run.status = 'failed'
            validation_run.success = False
            validation_run.error_message = str(e)
            validation_run.output = traceback.format_exc()
            validation_run.completed_at = timezone.now()
            validation_run.save()
        except Exception:
            pass

        raise e


@shared_task(bind=True)
def run_visualization_task(self, visualization_run_id):
    """
    Background task to execute a user's data visualization script.
    Similar logic to run_validation_task, but focused on generating plots.
    """
    try:
        # 1. Setup
        visualization_run = VisualizationRun.objects.get(
            id=visualization_run_id)
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

        # 2. Execution
        with VisualizationContext(
            str(project.identifier),
            str(visualization_run_id)
        ) as context:
            script_content = project.data_visualization_script.read()
            if isinstance(script_content, bytes):
                script_content = script_content.decode('utf-8')

            with tempfile.NamedTemporaryFile(
                mode='w',
                suffix='.py',
                delete=False
            ) as script_file:
                # Wrap visualization script with helper for saving plots
                script_wrapper = f"""
# Auto-injected visualization context
import sys
import os
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend for headless execution
import matplotlib.pyplot as plt

class VisualizationHelper:
    def __init__(self, viz_context):
        self._context = viz_context

    def save_plot(self, title="Untitled Plot"):
        self._context.save_plot(title)

    def open(self, relative_path, mode='r', **kwargs):
        return self._context.open(relative_path, mode, **kwargs)

    def exists(self, relative_path):
        return self._context.exists(relative_path)

    def listdir(self, relative_path=""):
        return self._context.listdir(relative_path)

    def get_data_path(self, relative_path=""):
        return self._context.get_data_path(relative_path)

# Initialize context (available as 'visualization')
_helper = VisualizationHelper(_context)
visualization = _helper

# --- User script starts here ---
{script_content}
"""
                script_file.write(script_wrapper)
                script_file.flush()

                try:
                    script_globals = {
                        '_context': context,
                        '__file__': script_file.name,
                        '__name__': '__main__'
                    }

                    exec(
                        compile(script_wrapper, script_file.name, 'exec'),
                        script_globals
                    )

                    visualization_run.success = True
                    visualization_run.output = (
                        f"Generated {len(context.plots)} plots."
                    )

                except Exception as e:
                    visualization_run.success = False
                    visualization_run.error_message = str(e)
                    visualization_run.output = traceback.format_exc()
                    log.data.error(f"Visualization script failed: {str(e)}")

                finally:
                    try:
                        os.unlink(script_file.name)
                    except Exception:
                        pass

            # 3. Save Plots
            for plot_data in context.plots:
                VisualizationPlot.objects.create(
                    visualization_run=visualization_run,
                    **plot_data
                )

        # 4. Finalize
        visualization_run.status = 'completed'
        visualization_run.completed_at = timezone.now()
        visualization_run.save()

        log.data.info("Visualization run finished")

        return {
            'success': visualization_run.success,
            'plots_count': len(context.plots),
            'output': visualization_run.output
        }

    except Exception as e:
        try:
            visualization_run = VisualizationRun.objects.get(
                id=visualization_run_id
            )
            visualization_run.status = 'failed'
            visualization_run.success = False
            visualization_run.error_message = str(e)
            visualization_run.output = traceback.format_exc()
            visualization_run.completed_at = timezone.now()
            visualization_run.save()
        except Exception:
            pass

        raise e
