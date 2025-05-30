import io
import base64
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import matplotlib.pyplot as plt
from .filesystem import DataFileSystem
from apps.logs import logger

class VisualizationContext:
    """Context providing access to data and utilities for visualization scripts."""
    
    def __init__(self, project_uuid: str, visualization_run_id: str):
        self.project_uuid = project_uuid
        self.visualization_run_id = visualization_run_id
        self.filesystem = DataFileSystem(project_uuid)
        self.plots = []
        self.current_plot_number = 0
        self.log = logger.get_logger()
        
    def __enter__(self):
        self.filesystem.__enter__()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.log.data.error("Visualization context exited with error",
                                 error_type=str(exc_type),
                                 error_message=str(exc_val))
            
        self.filesystem.__exit__(exc_type, exc_val, exc_tb)
        
    def save_plot(self, title="Untitled Plot"):
        """Save the current matplotlib plot."""
        if self.current_plot_number >= 4:
            self.log.data.warning(f"Maximum of 4 plots allowed. Plot '{title}' will be ignored.",
                                   current_plots=self.current_plot_number,
                                   max_plots=4,
                                   rejected_title=title)
            return
        
        self.current_plot_number += 1
        
        try:
            # Save the current figure to a BytesIO buffer
            buffer = io.BytesIO()
            plt.savefig(buffer, format='png', dpi=100, bbox_inches='tight', transparent=True)
            buffer.seek(0)
            
            # Encode as base64
            image_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            image_size = len(image_base64)
            
            # Store plot data
            self.plots.append({
                'title': title,
                'plot_number': self.current_plot_number,
                'image_data': image_base64
            })
            
            # Clear the current figure for the next plot
            plt.clf()

            self.log.data.info(f"Plot {self.current_plot_number}: '{title}' saved successfully",
                               plot_number=self.current_plot_number,
                               title=title,
                               image_size_bytes=image_size)

        except Exception as e:
            self.log.data.error(f"Failed to save plot '{title}': {str(e)}",
                                plot_number=self.current_plot_number,
                                title=title,
                                 error=str(e))
            raise
    
    def open(self, relative_path: str, mode: str = 'r', **kwargs):
        """Open a file with the given mode."""
        
        try:
            file_handle = self.filesystem.open(relative_path, mode, **kwargs)
            
            return file_handle
        except Exception as e:
            self.log.data.error(f"Failed to open file '{relative_path}': {str(e)}",
                                mode=mode,
                                error=str(e))
            raise
    
    def exists(self, relative_path: str) -> bool:
        """Check if a file exists in the data directory."""
        exists = self.filesystem.exists(relative_path)
        return exists
    
    def listdir(self, relative_path: str = "") -> list:
        """List files and directories in the given path."""
        try:
            files = self.filesystem.listdir(relative_path)
            return files
        except Exception as e:
            self.log.data.error(f"Failed to list directory '{relative_path}': {str(e)}")
            raise

    def get_data_path(self, relative_path: str = "") -> str:
        """Get local filesystem path to data."""
        if relative_path:
            full_path = self.filesystem.get_path(relative_path)
        else:
            full_path = self.filesystem.temp_dir
        return full_path
