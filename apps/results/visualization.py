import io
import os
import base64
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import matplotlib.pyplot as plt
import torch
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
        """Save the current matplotlib plot."""
        if self.current_plot_number >= 4:
            self.log.results.warning(f"Maximum of 4 plots allowed. Plot '{title}' will be ignored.")
            return

        self.current_plot_number += 1

        try:
            buffer = io.BytesIO()
            plt.savefig(buffer, format='png', dpi=100, bbox_inches='tight', transparent=True)
            buffer.seek(0)
            image_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

            self.plots.append({
                'title': title,
                'plot_number': self.current_plot_number,
                'image_data': image_base64
            })
            plt.clf()
            self.log.results.info(f"Plot {self.current_plot_number}: '{title}' saved successfully")
        except Exception as e:
            self.log.results.error(f"Failed to save plot '{title}': {str(e)}")
            raise

    def get_model(self, client_name=None, model_filename=None):
        """
        Load and return the trained model.
        Tries to find the model in S3 results first, then local workspace.
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
            # S3 Result path
            search_paths.append(f"{self.project_uuid}/results/{flare_id}/{client_name}/{model_filename}")
            # Local workspace path
            search_paths.append(os.path.join('workspaces', self.project_uuid, str(self.job.network.identifier), 'workspace', flare_id, client_name, "models", model_filename))

        # Default candidates based on common naming patterns mentioned by user
        #! update these patterns as needed
        candidates = [
            # Pattern: results/<job_id>/app_<client>/FL_global_model.pt
            f"{self.project_uuid}/results/{flare_id}/fl-client-2/app_fl-client-2/FL_global_model.pt",
            f"{self.project_uuid}/results/{flare_id}/fl-client-1/app_fl-client-1/FL_global_model.pt",
            # Pattern: results/<job_id>/<client>/model.pt
            f"{self.project_uuid}/results/{flare_id}/fl-client-1/model.pt",
            f"{self.project_uuid}/results/{flare_id}/fl-client-2/model.pt",
        ]
        
        for path in candidates:
            if path not in search_paths:
                search_paths.append(path)

        # 3. Try to find and load
        for path in search_paths:
            try:
                if path.startswith(self.project_uuid): # S3 path
                    if default_storage.exists(path):
                        self.log.results.info(f"Found model in S3: {path}")
                        with default_storage.open(path, 'rb') as s3_file:
                            with tempfile.NamedTemporaryFile(suffix='.pt', delete=False) as tmp:
                                shutil.copyfileobj(s3_file, tmp)
                                tmp_path = tmp.name
                        try:
                            model = torch.load(tmp_path, map_location=torch.device('cpu'))
                            os.unlink(tmp_path)
                            return model
                        except Exception as load_err:
                            if os.path.exists(tmp_path): os.unlink(tmp_path)
                            self.log.results.warning(f"Failed to load model from {path}: {load_err}")
                            continue
                else: # Local path
                    if os.path.exists(path):
                        self.log.results.info(f"Found model locally: {path}")
                        return torch.load(path, map_location=torch.device('cpu'))
            except Exception as e:
                continue

        # 4. Final attempt: Scan the results directory for ANY .pt file
        try:
            from apps.data.utils import get_s3_client
            from django.conf import settings
            s3 = get_s3_client()
            results_prefix = f"{self.project_uuid}/results/{flare_id}/"
            paginator = s3.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=settings.AWS_STORAGE_BUCKET_NAME, Prefix=results_prefix):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if key.endswith('.pt'):
                        self.log.results.info(f"Found .pt file by scanning S3 results: {key}")
                        with default_storage.open(key, 'rb') as s3_file:
                            with tempfile.NamedTemporaryFile(suffix='.pt', delete=False) as tmp:
                                shutil.copyfileobj(s3_file, tmp)
                                tmp_path = tmp.name
                        model = torch.load(tmp_path, map_location=torch.device('cpu'))
                        os.unlink(tmp_path)
                        return model
        except Exception as scan_err:
            self.log.results.error(f"Error scanning results folder for models: {scan_err}")

        raise FileNotFoundError(f"Could not find model file for job {flare_id} in results or workspace.")

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
