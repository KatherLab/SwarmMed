"""Celery background tasks for the results application."""

import base64
import hashlib
import json
import os
import textwrap

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from celery import shared_task
from common.utils import get_s3_client
from data.sandbox import run_script_in_sandbox
from logs import logger
from logs.context import set_context
from logs.utils import format_exception
from project.models import Project
from training.models import TrainingJob
from training.utils import extract_flare_job_uuid, upload_file_to_s3

from .artifacts import (
    GLOBAL_MODEL_FILENAME,
    is_model_artifact,
    parse_result_key,
)
from .models import (
    ResultsVisualizationPlot,
    ResultsVisualizationRun,
    TrainingResult,
)
from .visualization import ResultsVisualizationContext


def _file_md5(path: str) -> str:
    """Return the hex MD5 digest for a local file."""
    digest = hashlib.md5()  # nosec B324 - checksum reporting, not security.
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_workspace_base(project_uuid: str, network_uuid: str) -> str | None:
    """Locate the NVFlare prod workspace for a project/network pair."""
    workspace_root = os.path.join(
        settings.BASE_DIR, "workspaces", project_uuid, network_uuid
    )
    if not os.path.exists(workspace_root):
        return None

    for root, dirs, _ in os.walk(workspace_root):
        if "prod_00" in dirs:
            return os.path.join(root, "prod_00")
    return None


def _select_result_root(target: str, app_subdir: str) -> str:
    """Prefer the participant job root when it contains result artifacts."""
    app_global_model = os.path.join(app_subdir, GLOBAL_MODEL_FILENAME)
    if os.path.exists(app_global_model):
        return app_subdir
    target_global_model = os.path.join(target, GLOBAL_MODEL_FILENAME)
    if os.path.exists(target_global_model):
        return target

    artifact_markers = {
        "audit.log",
        "fl_app.txt",
        "log.json",
        "log.txt",
        "log_error.txt",
        "log_fl.txt",
        "meta.json",
        "stats_pool_summary.json",
    }
    if os.path.isdir(os.path.join(target, "models")):
        return target
    if os.path.exists(target):
        try:
            if artifact_markers.intersection(os.listdir(target)):
                return target
        except OSError:
            pass
    if os.path.exists(app_subdir):
        return app_subdir
    return target


def _iter_job_result_folders(job: TrainingJob):
    """Yield participant result folders for a completed training job."""
    flare_job_uuid = job.flare_job_uuid or extract_flare_job_uuid(
        job.flare_job_id or ""
    )
    if not flare_job_uuid:
        return

    workspace_base = _find_workspace_base(
        str(job.project.identifier), str(job.network.identifier)
    )
    if not workspace_base:
        return

    found_folders = []
    job_root = os.path.join(workspace_base, flare_job_uuid)
    if os.path.exists(job_root):
        for item in os.listdir(job_root):
            if item.startswith("app_"):
                participant = item[4:]
                found_folders.append((participant, os.path.join(job_root, item)))

    if not found_folders:
        for participant in os.listdir(workspace_base):
            participant_path = os.path.join(workspace_base, participant)
            if (
                not os.path.isdir(participant_path)
                or participant.lower()
                in {"admin", "startup", "logs", "local", "transfer", "custom"}
            ):
                continue

            target = os.path.join(participant_path, flare_job_uuid)
            if not os.path.exists(target):
                continue

            app_subdir = os.path.join(target, f"app_{participant}")
            found_folders.append(
                (participant, _select_result_root(target, app_subdir))
            )

    for participant, local_path in found_folders:
        yield flare_job_uuid, participant, local_path


def _sync_local_workspace_results(project: Project, log) -> None:
    """Upload local NVFlare model artifacts into canonical object storage."""
    for job in (
        TrainingJob.objects.filter(project=project, status="COMPLETED")
        .select_related("project", "network")
        .order_by("-created_at")
    ):
        for flare_job_uuid, participant, local_path in _iter_job_result_folders(
            job
        ):
            uploaded_any = False
            for root, _dirs, files in os.walk(local_path):
                for filename in files:
                    if not is_model_artifact(filename):
                        continue
                    local_artifact_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(
                        local_artifact_path, local_path
                    ).replace(os.sep, "/")
                    s3_key = (
                        f"{project.identifier}/results/{flare_job_uuid}/"
                        f"{participant}/{rel_path}"
                    )
                    upload_file_to_s3(
                        settings.AWS_STORAGE_BUCKET_NAME,
                        s3_key,
                        local_artifact_path,
                    )
                    md5 = _file_md5(local_artifact_path)
                    uploaded_any = True
                    log.results.info(
                        "Uploaded model artifact for "
                        f"job {job.identifier} participant {participant}: "
                        f"{rel_path} md5={md5}"
                    )

            if not uploaded_any:
                log.results.warning(
                    "No model artifacts found for "
                    f"job {job.identifier} participant {participant}: {local_path}"
                )


@shared_task
def sync_project_results(project_uuid):
    """Scans the S3 results folder for a project and updates our database.

    Ensures that files generated by training are visible in the UI.
    """
    log = logger.get_logger()
    try:
        project = Project.objects.get(identifier=project_uuid)
        log.results.info(f"Starting results sync for project {project_uuid}")
        _sync_local_workspace_results(project, log)

        job_lookup = {}
        for job in TrainingJob.objects.filter(project=project).only(
            "id", "project", "flare_job_id", "flare_job_uuid"
        ):
            job_uuid = job.flare_job_uuid or extract_flare_job_uuid(
                job.flare_job_id or ""
            )
            if job_uuid:
                job_lookup[job_uuid] = job

        s3 = get_s3_client()
        prefix = f"{project.identifier}/results/"
        paginator = s3.get_paginator("list_objects_v2")

        for page in paginator.paginate(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=prefix
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                filename = os.path.basename(key)
                if filename.startswith(".") or filename.endswith((".py", ".pyc")):
                    continue
                if not is_model_artifact(filename):
                    continue

                parsed = parse_result_key(key, str(project.identifier))
                if not parsed:
                    continue

                job_id_from_s3_key = parsed.flare_job_id
                normalized_s3_uuid = (
                    extract_flare_job_uuid(job_id_from_s3_key)
                    or job_id_from_s3_key
                )

                job = job_lookup.get(normalized_s3_uuid)
                if not job:
                    job = (
                        TrainingJob.objects.filter(
                            project=project, flare_job_uuid=normalized_s3_uuid
                        )
                        .order_by("created_at", "id")
                        .first()
                    )
                    if not job:
                        job = (
                            TrainingJob.objects.filter(
                                project=project,
                                flare_job_id__icontains=normalized_s3_uuid,
                            )
                            .order_by("created_at", "id")
                            .first()
                        )
                    if not job:
                        continue

                res_obj, created = TrainingResult.objects.get_or_create(
                    job=job,
                    file_path=key,
                    defaults={"file_size": obj.get("Size", 0)}
                )
                if not created:
                    res_obj.file_size = obj.get("Size", 0)
                    res_obj.save()
        log.results.info(f"Results sync completed for project {project_uuid}")
    except Exception as e:
        log.results.error(f"Failed to sync results: {format_exception(e)}")


@shared_task
def run_results_visualization_task(run_id, flare_id):
    """Background task to execute a results visualization script with fsspec streaming."""
    from common.utils import get_internal_s3_download_url
    log = logger.get_logger()
    try:
        run = ResultsVisualizationRun.objects.get(id=run_id)
        project = run.project
        job = run.job
        user = run.user
        set_context(user=user, project=project)

        run.status = "running"
        run.started_at = timezone.now()
        run.save()

        # Job might be None if visualizing S3-only results
        job_id_str = str(job.identifier) if job else flare_id
        
        with ResultsVisualizationContext(str(project.identifier), job_id_str, str(run.id)) as context:
            # Build manifest for both project data and results data
            manifest = context.filesystem.build_manifest()
            
            # Use the provided flare_id which is already processed by the view
            results_prefix = f"{project.identifier}/results/{flare_id}/"
            
            log.results.info(f"Building results manifest with prefix: {results_prefix}")
            
            s3 = get_s3_client()
            paginator = s3.get_paginator("list_objects_v2")
            found_count = 0
            for page in paginator.paginate(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=results_prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.endswith(("/", ".py", ".pyc")): continue
                    rel_path = key[len(results_prefix):]
                    download_url = get_internal_s3_download_url(key, expires=3600)
                    manifest[f"results/{rel_path}"] = download_url
                    found_count += 1
                    log.results.debug(f"Added to manifest: results/{rel_path}")

            log.results.info(f"Found {found_count} result files in S3.")

            manifest_path = os.path.join(context.filesystem.temp_dir, "data_manifest.json")
            with open(manifest_path, "w") as f:
                json.dump(manifest, f, indent=2)

            script_content = project.results_visualization_script.read()
            if isinstance(script_content, bytes):
                script_content = script_content.decode("utf-8")

            # Properly indent the user script for the try block
            indented_script = textwrap.indent(script_content, "    ")

            # Wrapper for Results Visualization
            script_wrapper = f"""
import json
import os
import io
import base64
import fsspec
import torch
import numpy as np
import traceback
import pickle
import matplotlib.pyplot as plt
from urllib.parse import urlparse

class ResultsVisualizationHelper:
    def __init__(self, manifest_file, plots_dir):
        print("--- ResultsVisualizationHelper Initializing ---")
        with open(manifest_file) as f:
            manifest = json.load(f)
        self.plots_dir = plots_dir
        self.plot_count = 0
        # Initialize internal streaming filesystem.
        # Request-level SSL verification is handled in self.open().
        self.fs = fsspec.filesystem("http")
        self.manifest = self._process_manifest(manifest)
        print(f"Manifest keys: {{list(self.manifest.keys())}}")
        print("--- ResultsVisualizationHelper Ready ---")

    def _process_manifest(self, manifest):
        internal_host = "minio"
        # Since we use network_mode="host" in sandbox, we rewrite URLs to minio:9000
        if not manifest: return {{}}
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

    def get_model(self, client_name="fl-client-1", model_filename="model.pt"):
        # Look for model in results/ prefix
        path = f"results/{{client_name}}/{{model_filename}}"
        print(f"Searching for model at: {{path}}")
        
        if path not in self.manifest:
            print(f"Path {{path}} not found, trying fallback...")
            # Fallback to any common model file extension
            extensions = (".pt", ".pth", ".ckpt", ".npy", ".npz", ".pkl", ".joblib", ".h5", ".keras")
            for k in self.manifest:
                if k.startswith("results/") and k.lower().endswith(extensions):
                    print(f"Fallback found model at: {{k}}")
                    path = k
                    break
        
        if path not in self.manifest:
            print("ERROR: No model file found in results/ prefix of manifest.")
            raise FileNotFoundError("Model weights not found in manifest.")
            
        url = self.manifest[path]
        print(f"Loading model from: {{url}}")
        try:
            # Handle Keras formats which often require a local file path
            if path.lower().endswith((".h5", ".keras")):
                import keras
                temp_path = os.path.join("/tmp", os.path.basename(path))
                # We pass ssl=False to support internal MinIO.
                with self.fs.open(url, "rb", ssl=False) as remote_f, open(temp_path, "wb") as local_f:
                    local_f.write(remote_f.read())
                model = keras.models.load_model(temp_path)
                return model.get_weights()

            # Handle standard streaming formats
            # We pass ssl=False to support internal MinIO.
            with self.fs.open(url, "rb", ssl=False) as f:
                if path.lower().endswith((".pt", ".pth", ".ckpt")):
                    data = torch.load(f, map_location='cpu', weights_only=True)
                    if isinstance(data, dict):
                        # Handle NVFlare or Lightning wrappers
                        data = data.get("numpy_key", data.get("weights", data.get("model", data.get("state_dict", data))))
                    return data
                elif path.lower().endswith(".npy"):
                    return np.load(f, allow_pickle=False)
                elif path.lower().endswith(".npz"):
                    d = np.load(f, allow_pickle=False)
                    return d.get("params", d.get("weights", d))
                elif path.lower().endswith((".pkl", ".joblib")):
                    try:
                        import joblib
                        return joblib.load(f)
                    except ImportError:
                        return pickle.load(f)
        except Exception as e:
            print(f"ERROR loading model {{url}}: {{e}}")
            traceback.print_exc()
            raise
        return None

    def load_weights(self, model, client_name="fl-client-1", model_filename="model.pt"):
        weights = self.get_model(client_name, model_filename)
        if weights is None: return False

        if hasattr(model, "load_state_dict"):
            # PyTorch
            if isinstance(weights, dict):
                state_dict = {{k: torch.as_tensor(v) for k, v in weights.items()}}
                model.load_state_dict(state_dict, strict=False)
                return True
        elif hasattr(model, "set_weights"):
            # Keras
            if isinstance(weights, dict):
                try:
                    keys = sorted(weights.keys(), key=lambda x: int(x))
                    weights = [np.array(weights[k]) for k in keys]
                except:
                    weights = [np.array(v) for k, v in sorted(weights.items())]
            model.set_weights(weights)
            return True
        elif hasattr(model, "coef_"):
            # Scikit-learn
            if isinstance(weights, dict):
                if "coef" in weights: model.coef_ = weights["coef"]
                if "intercept" in weights: model.intercept_ = weights["intercept"]
                return True
        return False

    def save_plot(self, title="Untitled Plot"):
        if self.plot_count >= 4: return
        self.plot_count += 1
        png_buf = io.BytesIO()
        plt.savefig(png_buf, format='png', dpi=100, bbox_inches='tight', transparent=True)
        png_data = base64.b64encode(png_buf.getvalue()).decode('utf-8')
        svg_buf = io.BytesIO()
        plt.savefig(svg_buf, format='svg', bbox_inches='tight', transparent=True)
        svg_data = base64.b64encode(svg_buf.getvalue()).decode('utf-8')
        plot_data = {{'title': title, 'plot_number': self.plot_count, 'image_data': png_data, 'svg_data': svg_data}}
        with open(os.path.join(self.plots_dir, f'plot_{{self.plot_count}}.json'), 'w') as f:
            json.dump(plot_data, f)
        plt.clf()

    def open(self, relative_path, mode='r', **kwargs):
        path = relative_path.lstrip("/")
        if path not in self.manifest: raise FileNotFoundError(f"File {{path}} not in manifest.")
        url = self.manifest[path]
        print(f"Opening streaming connection to: {{url}}")
        try:
            # We pass ssl=False to individual requests to support internal MinIO.
            kwargs.setdefault("ssl", False)
            return self.fs.open(url, mode=mode, **kwargs)
        except Exception as e:
            print(f"ERROR opening {{url}}: {{e}}")
            traceback.print_exc()
            raise

    def exists(self, relative_path):
        return relative_path.lstrip("/") in self.manifest

    def listdir(self, relative_path=""):
        p = relative_path.lstrip("/").rstrip("/")
        if not p: return list(self.manifest.keys())
        prefix = p + "/"
        return [k[len(prefix):] for k in self.manifest.keys() if k.startswith(prefix)]

visualization = ResultsVisualizationHelper('/home/sandboxuser/data/data_manifest.json', 'plots')

# --- User script ---
try:
{indented_script}
except Exception as e:
    print(f"CRITICAL ERROR in visualization script: {{e}}")
    traceback.print_exc()
"""

            result = run_script_in_sandbox(
                script_wrapper,
                context.filesystem.temp_dir,
                str(project.identifier),
                run_type="results_visualization",
            )

            run.success = result["success"]
            run.output = result["output"]
            if not result["success"]:
                run.error_message = result.get("error", "Sandbox execution failed")
                # Log the failure output to the results logger
                log.results.error(f"Results visualization sandbox failed for job {flare_id}:\n{run.output}")
            else:
                # Log successful output to results logger
                log.results.info(f"Results visualization sandbox completed for job {flare_id}:\n{run.output}")

            for plot_data in result.get("plots", []):
                plot_obj = ResultsVisualizationPlot(
                    visualization_run=run,
                    title=plot_data["title"],
                    plot_number=plot_data["plot_number"],
                )
                if plot_data.get("image_data"):
                    img_name = f"plot_{plot_data['plot_number']}.png"
                    plot_obj.image_data.save(img_name, ContentFile(base64.b64decode(plot_data["image_data"]), name=img_name), save=False)
                if plot_data.get("svg_data"):
                    svg_name = f"plot_{plot_data['plot_number']}.svg"
                    plot_obj.svg_data.save(svg_name, ContentFile(base64.b64decode(plot_data["svg_data"]), name=svg_name), save=False)
                plot_obj.save()

        run.status = "completed"
        run.completed_at = timezone.now()
        run.save()
        log.results.info(f"Results visualization task finished for job {flare_id}.")
        return {"success": run.success}

    except Exception as e:
        log.results.error(f"Results visualization task failed: {e}")
        try:
            run = ResultsVisualizationRun.objects.get(id=run_id)
            run.status = "failed"
            run.error_message = str(e)
            run.save()
        except: pass
        raise e
