import io
import base64
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import matplotlib.pyplot as plt
from .filesystem import DataFileSystem

class VisualizationContext:
    """Context providing access to data and utilities for visualization scripts."""
    
    def __init__(self, project_uuid: str, visualization_run_id: str):
        self.project_uuid = project_uuid
        self.visualization_run_id = visualization_run_id
        self.filesystem = DataFileSystem(project_uuid)
        self.plots = []
        self.current_plot_number = 0
        
    def __enter__(self):
        self.filesystem.__enter__()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.filesystem.__exit__(exc_type, exc_val, exc_tb)
    
    def save_plot(self, title="Untitled Plot"):
        """Save the current matplotlib plot."""
        if self.current_plot_number >= 4:
            print(f"Warning: Maximum of 4 plots allowed. Plot '{title}' will be ignored.")
            return
        
        self.current_plot_number += 1
        
        # Save the current figure to a BytesIO buffer
        buffer = io.BytesIO()
        plt.savefig(buffer, format='png', dpi=100, bbox_inches='tight', transparent=True)
        buffer.seek(0)
        
        # Encode as base64
        image_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        
        # Store plot data
        self.plots.append({
            'title': title,
            'plot_number': self.current_plot_number,
            'image_data': image_base64
        })
        
        # Clear the current figure for the next plot
        plt.clf()
        
        print(f"Plot {self.current_plot_number}: '{title}' saved successfully")
    
    def open(self, relative_path: str, mode: str = 'r', **kwargs):
        """Open a file with the given mode."""
        return self.filesystem.open(relative_path, mode, **kwargs)
    
    def exists(self, relative_path: str) -> bool:
        """Check if a file exists in the data directory."""
        return self.filesystem.exists(relative_path)
    
    def listdir(self, relative_path: str = "") -> list:
        """List files and directories in the given path."""
        return self.filesystem.listdir(relative_path)
    
    def get_data_path(self, relative_path: str = "") -> str:
        """Get local filesystem path to data."""
        if relative_path:
            return self.filesystem.get_path(relative_path)
        else:
            return self.filesystem.temp_dir
