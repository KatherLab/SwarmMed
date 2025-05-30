import os
import traceback
import tempfile
from datetime import datetime
from celery import shared_task
from django.utils import timezone
from django.contrib.auth.models import User
from .models import ValidationRun, ValidationCheck, VisualizationRun, VisualizationPlot
from .filesystem import ValidationContext
from ..project.models import Project
from .visualization import VisualizationContext
from ..logs import logger
from ..logs.context import set_context


@shared_task(bind=True)
def run_validation_task(self, validation_run_id):
    """Celery task to run data validation script."""
    try:
        # Get validation run and set logging context
        validation_run = ValidationRun.objects.get(id=validation_run_id)
        project = validation_run.project
        user = validation_run.user
        
        # Set logging context for Celery
        set_context(user=user, project=project)
        log = logger.get_logger()
        
        log.data.info("Starting validation task", 
                        validation_run_id=validation_run_id,
                        celery_task_id=self.request.id)
        
        validation_run.status = 'running'
        validation_run.started_at = timezone.now()
        validation_run.celery_task_id = self.request.id
        validation_run.save()
        
        if not project.data_validation_script:
            log.data.error("No validation script found for project")
            raise Exception("No validation script found for project")
        
        log.data.info("Creating validation context", project_id=project.identifier)
        
        # Create validation context
        with ValidationContext(str(project.identifier), str(validation_run_id)) as context:
            # Read validation script
            script_content = project.data_validation_script.read()
            if isinstance(script_content, bytes):
                script_content = script_content.decode('utf-8')

            log.data.debug("Read validation script", 
                            script_size=len(script_content),
                            script_filename=project.data_validation_script.name)
            
            # Create temporary script file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as script_file:
                # Inject the context into the script
                script_with_context = f"""
# Auto-injected validation context
import sys
import os

class ValidationContext:
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
        return self.filesystem.get_path(relative_path) if relative_path else self.filesystem.temp_dir
    
    def open(self, relative_path, mode='r', **kwargs):
        return self.filesystem.open(relative_path, mode, **kwargs)
    
    def exists(self, relative_path):
        return self.filesystem.exists(relative_path)
    
    def listdir(self, relative_path=""):
        return self.filesystem.listdir(relative_path)

# Initialize context (this will be available to the user script)
_validation_context = ValidationContext(_filesystem, _checks)
validation = _validation_context

# User script starts here:
{script_content}
"""
                script_file.write(script_with_context)
                script_file.flush()

                log.data.info("Executing validation script", script_path=script_file.name)

                # Execute the script
                try:
                    # Create a namespace for execution
                    script_globals = {
                        '_filesystem': context.filesystem,
                        '_checks': context.checks,
                        '__file__': script_file.name,
                        '__name__': '__main__'
                    }
                    
                    # Execute the script
                    exec(compile(script_with_context, script_file.name, 'exec'), script_globals)
                    
                    # Mark as successful
                    validation_run.success = True
                    validation_run.output = "Validation completed successfully"

                    log.data.info("Validation script executed successfully", 
                                   checks_found=len(context.checks))
                    
                except Exception as e:
                    # Capture script execution error
                    validation_run.success = False
                    validation_run.error_message = str(e)
                    validation_run.output = traceback.format_exc()
                    
                    log.data.error("Validation script execution failed", 
                                    error=str(e),
                                    traceback=traceback.format_exc())
                
                finally:
                    # Clean up script file
                    try:
                        os.unlink(script_file.name)
                        log.data.debug("Cleaned up temporary script file")
                    except:
                        log.data.warning("Failed to clean up temporary script file")

            # Save validation checks
            for check_data in context.checks:
                ValidationCheck.objects.create(
                    validation_run=validation_run,
                    **check_data
                )
            
            log.data.info("Saved validation checks", 
                         checks_count=len(context.checks),
                         validation_run_id=validation_run_id)
        
        # Update validation run status
        validation_run.status = 'completed'
        validation_run.completed_at = timezone.now()
        validation_run.save()
        
        log.data.info("Validation task completed successfully",
                        validation_run_id=validation_run_id,
                        success=validation_run.success,
                        checks_count=len(context.checks))
        
        return {
            'success': validation_run.success,
            'checks_count': len(context.checks),
            'output': validation_run.output
        }
        
    except Exception as e:
        # Handle task-level errors
        try:
            validation_run = ValidationRun.objects.get(id=validation_run_id)
            validation_run.status = 'failed'
            validation_run.success = False
            validation_run.error_message = str(e)
            validation_run.output = traceback.format_exc()
            validation_run.completed_at = timezone.now()
            validation_run.save()

            log.data.error("Validation task failed completely",
                            validation_run_id=validation_run_id,
                            error=str(e))
        except:
            pass
        
        raise e

@shared_task(bind=True)
def run_visualization_task(self, visualization_run_id):
    """Celery task to run data visualization script."""
    try:
        # Get visualization run and set logging context
        visualization_run = VisualizationRun.objects.get(id=visualization_run_id)
        project = visualization_run.project
        user = visualization_run.user
        
        # Set logging context for Celery
        set_context(user=user, project=project)
        log = logger.get_logger()
        
        log.data.info("Starting visualization task", 
                        visualization_run_id=visualization_run_id,
                        celery_task_id=self.request.id)
        
        visualization_run.status = 'running'
        visualization_run.started_at = timezone.now()
        visualization_run.celery_task_id = self.request.id
        visualization_run.save()
        
        if not project.data_visualization_script:
            log.data.error("No visualization script found for project")
            raise Exception("No visualization script found for project")
        
        log.data.info("Creating visualization context", project_id=project.identifier)
        
        # Create visualization context
        with VisualizationContext(str(project.identifier), str(visualization_run_id)) as context:
            # Read visualization script
            script_content = project.data_visualization_script.read()
            if isinstance(script_content, bytes):
                script_content = script_content.decode('utf-8')

            log.data.debug("Read visualization script", 
                            script_size=len(script_content),
                            script_filename=project.data_visualization_script.name)
            
            # Create temporary script file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as script_file:
                # Inject the context into the script
                script_with_context = f"""
# Auto-injected visualization context
import sys
import os
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import matplotlib.pyplot as plt

class VisualizationContext:
    def __init__(self, visualization_context):
        self._context = visualization_context
        
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

# Initialize context (this will be available to the user script)
_viz_context = VisualizationContext(_visualization_context)
visualization = _viz_context

# User script starts here:
{script_content}
"""
                script_file.write(script_with_context)
                script_file.flush()

                log.data.info("Executing visualization script", script_path=script_file.name)

                # Execute the script
                try:
                    # Create a namespace for execution
                    script_globals = {
                        '_visualization_context': context,
                        '__file__': script_file.name,
                        '__name__': '__main__'
                    }
                    
                    # Execute the script
                    exec(compile(script_with_context, script_file.name, 'exec'), script_globals)
                    
                    # Mark as successful
                    visualization_run.success = True
                    visualization_run.output = f"Visualization completed successfully. Generated {len(context.plots)} plots."

                    log.data.info("Visualization script executed successfully",
                                   plots_generated=len(context.plots))
                    
                except Exception as e:
                    # Capture script execution error
                    visualization_run.success = False
                    visualization_run.error_message = str(e)
                    visualization_run.output = traceback.format_exc()

                    log.data.error("Visualization script execution failed",
                                    error=str(e),
                                    traceback=traceback.format_exc())
                
                finally:
                    # Clean up script file
                    try:
                        os.unlink(script_file.name)
                        log.data.debug("Cleaned up temporary script file")
                    except:
                        log.data.warning("Failed to clean up temporary script file")
            
            # Save visualization plots
            for plot_data in context.plots:
                VisualizationPlot.objects.create(
                    visualization_run=visualization_run,
                    **plot_data
                )

            log.data.info("Saved visualization plots",
                           plots_count=len(context.plots),
                           visualization_run_id=visualization_run_id)
        
        # Update visualization run status
        visualization_run.status = 'completed'
        visualization_run.completed_at = timezone.now()
        visualization_run.save()

        log.data.info("Visualization task completed successfully",
                      visualization_run_id=visualization_run_id,
                        success=visualization_run.success,
                        plots_count=len(context.plots))
        
        return {
            'success': visualization_run.success,
            'plots_count': len(context.plots),
            'output': visualization_run.output
        }
        
    except Exception as e:
        # Handle task-level errors
        try:
            visualization_run = VisualizationRun.objects.get(id=visualization_run_id)
            visualization_run.status = 'failed'
            visualization_run.success = False
            visualization_run.error_message = str(e)
            visualization_run.output = traceback.format_exc()
            visualization_run.completed_at = timezone.now()
            visualization_run.save()

            log.data.error("Visualization task failed completely",
                            visualization_run_id=visualization_run_id,
                            error=str(e))
        except:
            pass
        
        raise e
