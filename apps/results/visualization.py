import io
import os
import base64
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
matplotlib.rcParams['svg.fonttype'] = 'none'  # Save text as text, not paths
import matplotlib.pyplot as plt
import torch
import numpy as np
from apps.data.filesystem import DataFileSystem
from apps.logs import logger
from apps.training.models import TrainingJob


class ResultsVisualizationContext:
    """Context providing access to data, models, and utilities for results visualization scripts."""

    def __init__(self, project_uuid: str, job_identifier: str, run_id: str):
        self.project_uuid = project_uuid
        self.job_identifier = job_identifier
        self.run_id = run_id
        self.filesystem = DataFileSystem(project_uuid)
        self.plots = []
        self.current_plot_number = 0
        self.log = logger.get_logger()
        self.job = TrainingJob.objects.get(identifier=job_identifier)

    def __enter__(self):
        self.log.results.info(f"Entering ResultsVisualizationContext for project={self.project_uuid}, job={self.job_identifier}")
        self.filesystem.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.log.results.error(f"Results visualization context exited with error: {str(exc_val)}")
        else:
            self.log.results.info("Results visualization context exited successfully")
        self.filesystem.__exit__(exc_type, exc_val, exc_tb)

    def save_plot(self, title="Untitled Plot"):
        """Save the current matplotlib plot in PNG and SVG formats."""
        if self.current_plot_number >= 4:
            self.log.results.warning(f"Maximum of 4 plots allowed. Plot '{title}' will be ignored.")
            return

        self.current_plot_number += 1

        try:
            # PNG
            png_buffer = io.BytesIO()
            plt.savefig(png_buffer, format='png', dpi=100, bbox_inches='tight', transparent=True)
            png_buffer.seek(0)
            png_base64 = base64.b64encode(png_buffer.getvalue()).decode('utf-8')

            # SVG
            svg_buffer = io.BytesIO()
            plt.savefig(svg_buffer, format='svg', bbox_inches='tight', transparent=True)
            svg_buffer.seek(0)
            svg_base64 = base64.b64encode(svg_buffer.getvalue()).decode('utf-8')

            self.plots.append({
                'title': title,
                'plot_number': self.current_plot_number,
                'image_data': png_base64,
                'svg_data': svg_base64
            })
            plt.clf()
            self.log.results.info(f"Plot {self.current_plot_number}: '{title}' saved successfully (PNG + SVG)")
        except Exception as e:
            self.log.results.error(f"Failed to save plot '{title}': {str(e)}")
            raise

    def load_weights(self, model):
        """
        Load weights from the job results into the provided model instance.
        Handles PyTorch, Keras/TF, and Scikit-learn models automatically.
        """
        weights = self.get_model()
        
        # 1. PyTorch
        if hasattr(model, "load_state_dict"):
            state_dict = {k: torch.as_tensor(v) for k, v in weights.items()}
            model.load_state_dict(state_dict, strict=False)
            self.log.results.info("Successfully loaded weights into PyTorch model.")
            return True
            
        # 2. Keras / TensorFlow
        if hasattr(model, "set_weights"):
            if isinstance(weights, dict):
                try:
                    sorted_keys = sorted(weights.keys(), key=lambda x: int(x))
                    weights = [np.array(weights[k]) for k in sorted_keys]
                except:
                    weights = [np.array(v) for k, v in sorted(weights.items())]
            model.set_weights(weights)
            self.log.results.info("Successfully loaded weights into Keras/TF model.")
            return True

        # 3. Scikit-learn
        if hasattr(model, "coef_") or hasattr(model, "coef"):
            if isinstance(weights, dict):
                for k, v in weights.items():
                    if k in ['coef_', 'intercept_', 'coef', 'intercept']:
                        setattr(model, k, np.array(v))
                self.log.results.info("Successfully loaded weights into Scikit-learn model.")
                return True

        raise TypeError(f"Unsupported model type for load_weights: {type(model)}")

    def get_model(self, client_name=None, model_filename=None):
        """
        Load and return the trained model data.
        Automatically handles PyTorch (.pt) and Numpy (.npy, .npz) formats,
        and unwraps NVFlare-specific structures (ModelLearnable/DXO).
        """
        import ast
        from django.core.files.storage import default_storage
        import tempfile
        import shutil

        # 1. Parse clean flare_job_id
        flare_id = self.job.flare_job_id
        try:
            parsed = ast.literal_eval(flare_id)
            if isinstance(parsed, list):
                for it in parsed:
                    if isinstance(it, dict) and it.get('type') == 'string' and 'Submitted job:' in it.get('data', ''):
                        flare_id = it.get('data', '').split(':')[-1].strip()
                        break
        except:
            pass
        
        # 2. Define search candidates
        search_paths = []
        
        # If user provided specific names, prioritize them
        if client_name and model_filename:
            search_paths.append(f"{self.project_uuid}/results/{flare_id}/{client_name}/{model_filename}")
            search_paths.append(os.path.join('workspaces', self.project_uuid, str(self.job.network.identifier), 'workspace', flare_id, client_name, "models", model_filename))

        # Default candidates based on common naming patterns
        candidates = [
            f"{self.project_uuid}/results/{flare_id}/fl-client-2/app_fl-client-2/FL_global_model.pt",
            f"{self.project_uuid}/results/{flare_id}/fl-client-1/app_fl-client-1/FL_global_model.pt",
            f"{self.project_uuid}/results/{flare_id}/fl-client-1/model.npy",
            f"{self.project_uuid}/results/{flare_id}/fl-client-1/model.npz",
            f"{self.project_uuid}/results/{flare_id}/fl-client-1/model.pt",
            f"{self.project_uuid}/results/{flare_id}/fl-client-2/model.pt",
        ]
        
        for path in candidates:
            if path not in search_paths:
                search_paths.append(path)

        # 3. Try to find and load
        for path in search_paths:
            try:
                local_path = None
                if path.startswith(self.project_uuid): # S3 path
                    if default_storage.exists(path):
                        self.log.results.info(f"Found model in S3: {path}")
                        with default_storage.open(path, 'rb') as s3_file:
                            ext = os.path.splitext(path)[1]
                            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                                shutil.copyfileobj(s3_file, tmp)
                                local_path = tmp.name
                else: # Local path
                    if os.path.exists(path):
                        self.log.results.info(f"Found model locally: {path}")
                        local_path = path

                if local_path:
                    model_data = None
                    if local_path.endswith('.pt'):
                        model_data = torch.load(local_path, map_location=torch.device('cpu'))
                        if isinstance(model_data, dict):
                            if 'weights' in model_data: model_data = model_data['weights']
                            elif 'model' in model_data: model_data = model_data['model']
                            if isinstance(model_data, dict):
                                model_data = model_data.get('numpy_key', model_data)
                    elif local_path.endswith('.npy'):
                        model_data = np.load(local_path, allow_pickle=True)
                        if isinstance(model_data, np.ndarray) and model_data.dtype == object:
                            try:
                                d = model_data.item()
                                if isinstance(d, dict):
                                    model_data = d.get('numpy_key', d)
                            except: pass
                        elif isinstance(model_data, dict):
                            model_data = model_data.get('numpy_key', model_data)
                    elif local_path.endswith('.npz'):
                        model_data = np.load(local_path, allow_pickle=True)
                        model_data = model_data.get('params', model_data.get('weights', model_data))
                    
                    if model_data is not None:
                        if path.startswith(self.project_uuid) and local_path and os.path.exists(local_path):
                            try: os.unlink(local_path)
                            except: pass
                        return model_data
            except Exception as e:
                self.log.results.warning(f"Failed to load model from {path}: {e}")
                continue

        # 4. Final attempt: Scan the results directory for ANY model file
        try:
            from apps.data.utils import get_s3_client
            from django.conf import settings
            s3 = get_s3_client()
            results_prefix = f"{self.project_uuid}/results/{flare_id}/"
            paginator = s3.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=results_prefix):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if key.endswith(('.pt', '.npy', '.npz')):
                        self.log.results.info(f"Found model file by scanning S3 results: {key}")
                        return self.get_model(model_filename=os.path.basename(key), client_name=key.split('/')[3])
        except Exception as scan_err:
            self.log.results.error(f"Error scanning results folder for models: {scan_err}")

        raise FileNotFoundError(f"Could not find model weights for job {flare_id}. Ensure training completed and weights were synced.")

    def get_model_path(self, client_name=None, model_filename=None):
        """
        Download the model file and return its local filesystem path.
        Useful for loading models with framework-specific tools (e.g. tf.keras.models.load_model).
        """
        import ast
        from django.core.files.storage import default_storage
        import tempfile
        import shutil

        # Parse flare_job_id
        flare_id = self.job.flare_job_id
        try:
            parsed = ast.literal_eval(flare_id)
            if isinstance(parsed, list):
                for it in parsed:
                    if isinstance(it, dict) and it.get('type') == 'string' and 'Submitted job:' in it.get('data', ''):
                        flare_id = it.get('data', '').split(':')[-1].strip()
                        break
        except:
            pass

        if not client_name: client_name = "fl-client-1"
        if not model_filename: model_filename = "model.pt"

        s3_path = f"{self.project_uuid}/results/{flare_id}/{client_name}/{model_filename}"
        
        if default_storage.exists(s3_path):
            with default_storage.open(s3_path, 'rb') as s3_file:
                ext = os.path.splitext(model_filename)[1]
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                shutil.copyfileobj(s3_file, tmp)
                tmp.close()
                return tmp.name
        
        raise FileNotFoundError(f"Model file {model_filename} for client {client_name} not found in S3.")

    def open(self, relative_path: str, mode: str = 'r', **kwargs):
        """Open a file from the project's data directory."""
        return self.filesystem.open(relative_path, mode, **kwargs)

    def exists(self, relative_path: str) -> bool:
        """Check if a file exists in the project's data directory."""
        return self.filesystem.exists(relative_path)

    def listdir(self, relative_path: str = "") -> list:
        """List files and directories in the project's data directory."""
        return self.filesystem.listdir(relative_path)

    def get_data_path(self, relative_path: str = "") -> str:
        """Get local filesystem path to data in the project's data directory."""
        return self.filesystem.get_path(relative_path) if relative_path else self.filesystem.temp_dir