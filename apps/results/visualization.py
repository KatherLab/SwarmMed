"""
Visualization context and utilities for the results application.
Provides the bridge between user-written Python scripts and the project's
stored data and model weights.
"""

from apps.training.models import TrainingJob
from apps.logs import logger
from apps.data.filesystem import DataFileSystem
import base64
import io
import os

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import torch

# Use a non-interactive backend for Matplotlib to work in background tasks.
matplotlib.use('Agg')
# Save text in SVGs as text objects rather than paths for better accessibility.
matplotlib.rcParams['svg.fonttype'] = 'none'


class ResultsVisualizationContext:
    """
    Context manager that provides a safe and easy-to-use API for
    user-submitted visualization scripts.

    It handles:
    1. Setting up access to the project's data filesystem.
    2. Finding and loading trained model weights from S3 or local storage.
    3. Capturing and encoding Matplotlib plots to be stored in the database.
    """

    def __init__(self, project_uuid: str, job_identifier: str, run_id: str):
        """
        Initialize the context with project and job identifiers.
        """
        self.project_uuid = project_uuid
        self.job_identifier = job_identifier
        self.run_id = run_id

        # Initialize the data filesystem for this project.
        self.filesystem = DataFileSystem(project_uuid)

        # List to store captured plots before they are saved to the database.
        self.plots = []
        self.current_plot_number = 0

        self.log = logger.get_logger()

        # Fetch the training job to access its metadata (like flare_job_id).
        self.job = TrainingJob.objects.get(identifier=job_identifier)

    def __enter__(self):
        """Called when entering the 'with' block."""
        self.log.results.info(
            f"Entering ResultsVisualizationContext for project={self.project_uuid}, "
            f"job={self.job_identifier}")
        # Initialize the filesystem (e.g., creating temporary directories).
        self.filesystem.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Called when exiting the 'with' block, ensuring resources are cleaned up."""
        if exc_type:
            self.log.results.error(
                f"Results visualization context exited with error: {str(exc_val)}"
            )
        else:
            self.log.results.info(
                "Results visualization context exited successfully")

        # Clean up the filesystem.
        self.filesystem.__exit__(exc_type, exc_val, exc_tb)

    def save_plot(self, title="Untitled Plot"):
        """
        Captures the current Matplotlib figure, encodes it, and adds it to the list.
        Each script is allowed up to 4 plots.
        """
        if self.current_plot_number >= 4:
            self.log.results.warning(
                f"Maximum of 4 plots allowed. Plot '{title}' will be ignored."
            )
            return

        self.current_plot_number += 1

        try:
            # Capture plot as PNG (Base64 encoded)
            png_buffer = io.BytesIO()
            plt.savefig(
                png_buffer,
                format='png',
                dpi=100,
                bbox_inches='tight',
                transparent=True
            )
            png_buffer.seek(0)
            png_base64 = base64.b64encode(
                png_buffer.getvalue()).decode('utf-8')

            # Capture plot as SVG (Base64 encoded)
            svg_buffer = io.BytesIO()
            plt.savefig(
                svg_buffer,
                format='svg',
                bbox_inches='tight',
                transparent=True)
            svg_buffer.seek(0)
            svg_base64 = base64.b64encode(
                svg_buffer.getvalue()).decode('utf-8')

            self.plots.append({
                'title': title,
                'plot_number': self.current_plot_number,
                'image_data': png_base64,
                'svg_data': svg_base64
            })

            # Clear the figure so the next plot starts fresh.
            plt.clf()

            self.log.results.info(
                f"Plot {self.current_plot_number}: '{title}' saved successfully"
            )
        except Exception as e:
            self.log.results.error(f"Failed to save plot '{title}': {str(e)}")
            raise

    def load_weights(self, model):
        """
        Attempts to load model weights into the provided model object.
        Supported frameworks: PyTorch, Keras/TensorFlow, Scikit-learn.
        """
        # Retrieve weights using the automated get_model utility.
        weights = self.get_model()

        # 1. PyTorch model handling
        if hasattr(model, "load_state_dict"):
            # Ensure all values are converted to tensors.
            state_dict = {k: torch.as_tensor(v) for k, v in weights.items()}
            model.load_state_dict(state_dict, strict=False)
            self.log.results.info(
                "Successfully loaded weights into PyTorch model.")
            return True

        # 2. Keras / TensorFlow model handling
        if hasattr(model, "set_weights"):
            if isinstance(weights, dict):
                # If weights are a dict, they likely need to be sorted into a
                # list.
                try:
                    sorted_keys = sorted(weights.keys(), key=lambda x: int(x))
                    weights = [np.array(weights[k]) for k in sorted_keys]
                except (ValueError, TypeError):
                    weights = [np.array(v) for k, v in sorted(weights.items())]
            model.set_weights(weights)
            self.log.results.info(
                "Successfully loaded weights into Keras/TF model.")
            return True

        # 3. Scikit-learn model handling
        if hasattr(model, "coef_") or hasattr(model, "coef"):
            if isinstance(weights, dict):
                # Map specific keys to scikit-learn attributes.
                for k, v in weights.items():
                    if k in ['coef_', 'intercept_', 'coef', 'intercept']:
                        setattr(model, k, np.array(v))
                self.log.results.info(
                    "Successfully loaded weights into Scikit-learn model."
                )
                return True

        raise TypeError(
            f"Unsupported model type for load_weights: {type(model)}")

    def get_model(self, client_name=None, model_filename=None):
        """
        Loads and returns model weights (usually as a dictionary or numpy array).
        This method automatically scans common locations in S3 and local workspace
        to find the trained global model.
        """
        import ast
        import shutil
        import tempfile
        from django.core.files.storage import default_storage

        # 1. Determine the clean NVFlare job ID.
        flare_id = self.job.flare_job_id
        try:
            # Handle complex flare_job_id formats (sometimes stored as
            # stringified lists).
            parsed = ast.literal_eval(flare_id)
            if isinstance(parsed, list):
                for item in parsed:
                    if (isinstance(item, dict) and
                            item.get('type') == 'string' and
                            'Submitted job:' in item.get('data', '')):
                        flare_id = item.get('data', '').split(':')[-1].strip()
                        break
        except (ValueError, SyntaxError):
            pass

        # 2. Define a list of paths where weights might be stored.
        search_paths = []

        # If user provided specific names, prioritize them.
        if client_name and model_filename:
            search_paths.append(
                f"{self.project_uuid}/results/{flare_id}/{client_name}/{model_filename}"
            )
            # Check local workspace as well.
            search_paths.append(os.path.join(
                'workspaces', self.project_uuid,
                str(self.job.network.identifier),
                'workspace', flare_id, client_name, "models", model_filename
            ))

        # Default candidates based on NVFlare's standard naming and output
        # structure.
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

        # 3. Iterate through search paths and try to load the first valid one.
        for path in search_paths:
            try:
                local_path = None
                if path.startswith(self.project_uuid):
                    # Path is in S3 storage.
                    if default_storage.exists(path):
                        self.log.results.info(f"Found model in S3: {path}")
                        with default_storage.open(path, 'rb') as s3_file:
                            ext = os.path.splitext(path)[1]
                            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                                shutil.copyfileobj(s3_file, tmp)
                                local_path = tmp.name
                else:
                    # Path is on the local filesystem.
                    if os.path.exists(path):
                        self.log.results.info(f"Found model locally: {path}")
                        local_path = path

                if local_path:
                    model_data = None
                    if local_path.endswith('.pt'):
                        # Load PyTorch weights.
                        model_data = torch.load(
                            local_path, map_location='cpu', weights_only=True)
                        # Unwrap common wrappers.
                        if isinstance(model_data, dict):
                            if 'weights' in model_data:
                                model_data = model_data['weights']
                            elif 'model' in model_data:
                                model_data = model_data['model']

                            # Handle NVFlare DXO structures.
                            if isinstance(model_data, dict):
                                model_data = model_data.get(
                                    'numpy_key', model_data)

                    elif local_path.endswith('.npy'):
                        # Load Numpy weights.
                        model_data = np.load(local_path, allow_pickle=False)
                        if isinstance(
                                model_data,
                                np.ndarray) and model_data.dtype == object:
                            try:
                                d = model_data.item()
                                if isinstance(d, dict):
                                    model_data = d.get('numpy_key', d)
                            except (ValueError, TypeError):
                                pass
                        elif isinstance(model_data, dict):
                            model_data = model_data.get(
                                'numpy_key', model_data)

                    elif local_path.endswith('.npz'):
                        # Load Numpy compressed weights.
                        model_data = np.load(local_path, allow_pickle=False)
                        model_data = model_data.get(
                            'params',
                            model_data.get('weights', model_data)
                        )

                    if model_data is not None:
                        # Clean up temporary file if created.
                        if path.startswith(
                                self.project_uuid) and os.path.exists(local_path):
                            try:
                                os.unlink(local_path)
                            except OSError:
                                pass
                        return model_data
            except Exception as e:
                self.log.results.warning(
                    f"Failed to load model from {path}: {e}")
                continue

        # 4. Fallback: Scan S3 results directory for ANY supported model file.
        try:
            from django.conf import settings
            from apps.data.utils import get_s3_client

            s3 = get_s3_client()
            results_prefix = f"{self.project_uuid}/results/{flare_id}/"
            paginator = s3.get_paginator('list_objects_v2')

            for page in paginator.paginate(
                Bucket=settings.AWS_STORAGE_BUCKET_NAME,
                Prefix=results_prefix
            ):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if key.endswith(('.pt', '.npy', '.npz')):
                        self.log.results.info(
                            f"Found model file by scanning S3 results: {key}"
                        )
                        # Extract client name (assuming 4th part of key).
                        parts = key.split('/')
                        client_name = parts[3] if len(parts) > 3 else None
                        return self.get_model(
                            model_filename=os.path.basename(key),
                            client_name=client_name
                        )
        except Exception as scan_err:
            self.log.results.error(
                f"Error scanning results folder: {scan_err}")

        raise FileNotFoundError(
            f"Could not find model weights for job {flare_id}. "
            "Ensure training completed and weights were synced."
        )

    def get_model_path(self, client_name=None, model_filename=None):
        """
        Downloads a model file and returns its local path.
        Useful for framework loaders that require a file path instead of a dictionary.
        """
        import ast
        import shutil
        import tempfile
        from django.core.files.storage import default_storage

        # Parse flare_job_id
        flare_id = self.job.flare_job_id
        try:
            parsed = ast.literal_eval(flare_id)
            if isinstance(parsed, list):
                for item in parsed:
                    if (isinstance(item, dict) and
                            item.get('type') == 'string' and
                            'Submitted job:' in item.get('data', '')):
                        flare_id = item.get('data', '').split(':')[-1].strip()
                        break
        except (ValueError, SyntaxError):
            pass

        if not client_name:
            client_name = "fl-client-1"
        if not model_filename:
            model_filename = "model.pt"

        s3_path = (f"{self.project_uuid}/results/{flare_id}/"
                   f"{client_name}/{model_filename}")

        if default_storage.exists(s3_path):
            with default_storage.open(s3_path, 'rb') as s3_file:
                ext = os.path.splitext(model_filename)[1]
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                shutil.copyfileobj(s3_file, tmp)
                tmp.close()
                return tmp.name

        raise FileNotFoundError(
            f"Model file {model_filename} for client {client_name} not found in S3."
        )

    # Simplified wrappers for standard filesystem operations.

    def open(self, relative_path: str, mode: str = 'r', **kwargs):
        """Opens a file from the project's data directory."""
        return self.filesystem.open(relative_path, mode, **kwargs)

    def exists(self, relative_path: str) -> bool:
        """Checks if a file exists in the project's data directory."""
        return self.filesystem.exists(relative_path)

    def listdir(self, relative_path: str = "") -> list:
        """Lists files and directories in the project's data directory."""
        return self.filesystem.listdir(relative_path)

    def get_data_path(self, relative_path: str = "") -> str:
        """Returns the local filesystem path to a project data file."""
        if relative_path:
            return self.filesystem.get_path(relative_path)
        return self.filesystem.temp_dir
