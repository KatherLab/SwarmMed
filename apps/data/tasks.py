"""
Celery tasks for the data app.
Handles background execution of data validation and visualization scripts
to keep the web interface responsive during long-running computations.
"""

import base64
import json
import os
import traceback

from celery import shared_task
from django.core.files.base import ContentFile
from django.utils import timezone
from logs import logger
from logs.context import set_context
from logs.utils import format_exception

from .filesystem import DataFileSystem
from .models import (
    ValidationCheck,
    ValidationRun,
    VisualizationPlot,
    VisualizationRun,
)
from .sandbox import run_script_in_sandbox


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

        validation_run.status = "running"
        validation_run.started_at = timezone.now()
        validation_run.celery_task_id = self.request.id
        validation_run.save()

        if not project.data_validation_script:
            log.data.error("No validation script found for project")
            raise Exception("No validation script found for project")

        # Prepare the streaming filesystem manifest
        with DataFileSystem(str(project.identifier)) as fs:
            fs.build_manifest()
            manifest_path = os.path.join(fs.temp_dir, "data_manifest.json")
            fs.save_manifest(manifest_path)

            script_content = project.data_validation_script.read()
            if isinstance(script_content, bytes):
                script_content = script_content.decode("utf-8")

            # Wrap user script with helper for sandbox streaming
            script_wrapper = f"""
import json
import os
import fsspec
from urllib.parse import urlparse

class ValidationHelper:
    def __init__(self, manifest_file, output_file):
        with open(manifest_file) as f:
            manifest = json.load(f)
        self.output_file = output_file
        self.checks = []
        # Internal streaming filesystem with SSL verification disabled.
        self.fs = fsspec.filesystem("http", ssl=False)
        self.manifest = self._process_manifest(manifest)

    def _process_manifest(self, manifest):
        internal_host = "minio"
        first_url = next(iter(manifest.values()), "")
        scheme = "https" if first_url.startswith("https") else "http"
        internal_endpoint = f"{{scheme}}://{{internal_host}}:9000"
        
        updated = {{}}
        for rel_path, url in manifest.items():
            p = urlparse(url)
            old_base = f"{{p.scheme}}://{{p.netloc}}"
            new_url = url.replace(old_base, internal_endpoint)
            updated[rel_path] = new_url
        return updated

    def add_check(self, name, status, message="", details=None):
        self.checks.append({{
            'name': name,
            'status': status,
            'message': message,
            'details': details or {{}}
        }})
        self._save()

    def get_data_path(self, relative_path=""):
        if not relative_path: return "."
        return self.manifest.get(relative_path.lstrip("/"))

    def open(self, relative_path, mode='r', **kwargs):
        path = relative_path.lstrip("/")
        if path not in self.manifest:
            raise FileNotFoundError(f"File not in manifest: {{path}}")
        return self.fs.open(self.manifest[path], mode=mode, **kwargs)

    def exists(self, relative_path):
        return relative_path.lstrip("/") in self.manifest

    def listdir(self, relative_path=""):
        path = relative_path.lstrip("/").rstrip("/")
        if not path:
            return list(self.manifest.keys())
        prefix = path + "/"
        return [k[len(prefix):] for k in self.manifest.keys() if k.startswith(prefix)]

    def _save(self):
        with open(self.output_file, 'w') as f:
            json.dump({{'checks': self.checks}}, f)

validation = ValidationHelper('/home/sandboxuser/data/data_manifest.json', 'results.json')

# --- User script ---
{script_content}
"""

            # Run in sandbox
            result = run_script_in_sandbox(
                script_wrapper,
                fs.temp_dir,
                str(project.identifier),
                run_type="validation",
            )

            validation_run.success = result["success"]
            validation_run.output = result["output"]

            if not result["success"] and "error" in result:
                validation_run.error_message = result["error"]
                log.data.warning(f"Validation failed: {result['error']}")

            # Save Results
            for check_data in result.get("results", []):
                ValidationCheck.objects.create(
                    validation_run=validation_run, **check_data
                )

        validation_run.status = "completed"
        validation_run.completed_at = timezone.now()
        validation_run.save()

        log.data.info(
            f"Validation run {validation_run_id} completed. Success: {validation_run.success}"
        )

        return {"success": validation_run.success}

    except Exception as e:
        log = logger.get_logger()
        log.data.error(f"Validation task failed: {format_exception(e)}")
        
        try:
            run = ValidationRun.objects.get(id=validation_run_id)
            run.status = "failed"
            run.error_message = str(e)
            run.save()
        except:
            pass
        return {"success": False, "error": str(e)}


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

        visualization_run.status = "running"
        visualization_run.started_at = timezone.now()
        visualization_run.celery_task_id = self.request.id
        visualization_run.save()

        if not project.data_visualization_script:
            log.data.error("No visualization script found for project")
            raise Exception("No visualization script found for project")

        # Prepare streaming manifest
        with DataFileSystem(str(project.identifier)) as fs:
            fs.build_manifest()
            manifest_path = os.path.join(fs.temp_dir, "data_manifest.json")
            fs.save_manifest(manifest_path)

            script_content = project.data_visualization_script.read()
            if isinstance(script_content, bytes):
                script_content = script_content.decode("utf-8")

            # Wrap user script
            script_wrapper = f"""
import json
import os
import io
import base64
import fsspec
import matplotlib.pyplot as plt
from urllib.parse import urlparse

class VisualizationHelper:
    def __init__(self, manifest_file, plots_dir):
        with open(manifest_file) as f:
            manifest = json.load(f)
        self.plots_dir = plots_dir
        self.plot_count = 0
        # Internal streaming filesystem with SSL verification disabled
        self.fs = fsspec.filesystem("http", ssl=False)
        self.manifest = self._process_manifest(manifest)

    def _process_manifest(self, manifest):
        internal_host = "minio"
        first_url = next(iter(manifest.values()), "")
        scheme = "https" if first_url.startswith("https") else "http"
        internal_endpoint = f"{{scheme}}://{{internal_host}}:9000"
        
        updated = {{}}
        for rel_path, url in manifest.items():
            p = urlparse(url)
            old_base = f"{{p.scheme}}://{{p.netloc}}"
            new_url = url.replace(old_base, internal_endpoint)
            updated[rel_path] = new_url
        return updated

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
        if not relative_path: return "."
        return self.manifest.get(relative_path.lstrip("/"))

    def open(self, relative_path, mode='r', **kwargs):
        path = relative_path.lstrip("/")
        if path not in self.manifest:
            raise FileNotFoundError(f"File not in manifest: {{path}}")
        return self.fs.open(self.manifest[path], mode=mode, **kwargs)

    def exists(self, relative_path):
        return relative_path.lstrip("/") in self.manifest

    def listdir(self, relative_path=""):
        path = relative_path.lstrip("/").rstrip("/")
        if not path:
            return list(self.manifest.keys())
        prefix = path + "/"
        return [k[len(prefix):] for k in self.manifest.keys() if k.startswith(prefix)]

visualization = VisualizationHelper('/home/sandboxuser/data/data_manifest.json', 'plots')

# --- User script ---
{script_content}
"""

            # Run in sandbox
            result = run_script_in_sandbox(
                script_wrapper,
                fs.temp_dir,
                str(project.identifier),
                run_type="visualization",
            )

            visualization_run.success = result["success"]
            visualization_run.output = result["output"]

            if not result["success"] and "error" in result:
                visualization_run.error_message = result["error"]
                log.data.warning(f"Visualization failed: {result['error']}")

            # Save Plots
            for plot_data in result.get("plots", []):
                plot_obj = VisualizationPlot(
                    visualization_run=visualization_run,
                    title=plot_data["title"],
                    plot_number=plot_data["plot_number"],
                )

                if plot_data.get("image_data"):
                    img_name = f"plot_{plot_data['plot_number']}.png"
                    img_content = ContentFile(
                        base64.b64decode(plot_data["image_data"]),
                        name=img_name,
                    )
                    plot_obj.image_data.save(img_name, img_content, save=False)

                if plot_data.get("svg_data"):
                    svg_name = f"plot_{plot_data['plot_number']}.svg"
                    svg_content = ContentFile(
                        base64.b64decode(plot_data["svg_data"]),
                        name=svg_name,
                    )
                    plot_obj.svg_data.save(svg_name, svg_content, save=False)

                plot_obj.save()

        visualization_run.status = "completed"
        visualization_run.completed_at = timezone.now()
        visualization_run.save()

        log.data.info(
            f"Visualization run {visualization_run_id} completed. Success: {visualization_run.success}"
        )

        return {"success": visualization_run.success}

    except Exception as e:
        log = logger.get_logger()
        log.data.error(f"Visualization task failed: {format_exception(e)}")
        
        try:
            run = VisualizationRun.objects.get(id=visualization_run_id)
            run.status = "failed"
            run.error_message = str(e)
            run.save()
        except:
            pass
        return {"success": False, "error": str(e)}
