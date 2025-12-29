import io
import os
import base64
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import matplotlib.pyplot as plt
#import torch
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
        self.filesystem.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.log.results.error(f"Results visualization context exited with error: {str(exc_val)}")
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

    def get_model(self, client_name="fl-client-1", model_filename="model.pt"):
        """
        Load and return the trained model.
        Assumes PyTorch model saved with torch.save().
        """
        try:
            workspace_path = os.path.join(
                'workspaces',
                self.project_uuid,
                str(self.job.network.identifier),
                'workspace',
                self.job.flare_job_id,
                client_name,
                "models",
                model_filename
            )

            if not os.path.exists(workspace_path):
                self.log.results.error(f"Model file not found at: {workspace_path}")
                raise FileNotFoundError(f"Model file not found at: {workspace_path}")

            # Assuming the model was saved with torch.save()
            model = torch.load(workspace_path)
            self.log.results.info(f"Successfully loaded model from {workspace_path}")
            return model
        except Exception as e:
            self.log.results.error(f"Failed to load model: {str(e)}")
            raise

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
