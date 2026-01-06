"""
Visualization utilities for the data app.
Provides a context manager for executing visualization scripts,
handling data access, and capturing matplotlib plots as Base64.
"""

import base64
import io

import matplotlib
import matplotlib.pyplot as plt
from logs import logger

from .filesystem import DataFileSystem

# Use the 'Agg' backend for non-interactive (headless) environments
matplotlib.use("Agg")
# Save text as actual text objects in SVGs for better quality/editing
matplotlib.rcParams["svg.fonttype"] = "none"


class VisualizationContext:
    """
    Context manager that provides access to project data and utilities
    for user-defined visualization scripts.
    """

    def __init__(self, project_uuid: str, visualization_run_id: str):
        self.project_uuid = project_uuid
        self.visualization_run_id = visualization_run_id
        # Provide access to the virtual filesystem for reading data
        self.filesystem = DataFileSystem(project_uuid)
        # List to store plot data (Base64 strings)
        self.plots = []
        self.current_plot_number = 0
        self.log = logger.get_logger()

    def __enter__(self):
        """Initializes the filesystem context."""
        self.filesystem.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Cleans up the filesystem and logs any errors."""
        if exc_type:
            self.log.data.error(
                f"Visualization context exited with error: {str(exc_val)}"
            )
        self.filesystem.__exit__(exc_type, exc_val, exc_tb)

    def save_plot(self, title="Untitled Plot"):
        """
        Captures the current Matplotlib figure, encodes it as PNG and SVG,
        and clears the figure for the next plot.
        """
        # Limit to 4 plots per run to prevent excessive database/memory usage
        if self.current_plot_number >= 4:
            self.log.data.warning(
                f"Max plots (4) reached. '{title}' was not saved."
            )
            return

        self.current_plot_number += 1

        try:
            # 1. Capture and encode as PNG
            png_buffer = io.BytesIO()
            plt.savefig(
                png_buffer,
                format="png",
                dpi=100,
                bbox_inches="tight",
                transparent=True,
            )
            png_buffer.seek(0)
            png_base64 = base64.b64encode(png_buffer.getvalue()).decode(
                "utf-8"
            )

            # 2. Capture and encode as SVG
            svg_buffer = io.BytesIO()
            plt.savefig(
                svg_buffer, format="svg", bbox_inches="tight", transparent=True
            )
            svg_buffer.seek(0)
            svg_base64 = base64.b64encode(svg_buffer.getvalue()).decode(
                "utf-8"
            )

            # 3. Store the encoded data
            self.plots.append(
                {
                    "title": title,
                    "plot_number": self.current_plot_number,
                    "image_data": png_base64,
                    "svg_data": svg_base64,
                }
            )

            # Clear the current figure so the next plot starts fresh
            plt.clf()

            self.log.data.info(
                f"Saved plot {self.current_plot_number}: '{title}'"
            )

        except Exception as e:
            self.log.data.error(f"Failed to save plot '{title}': {str(e)}")
            raise

    def open(self, relative_path: str, mode: str = "r", **kwargs):
        """
        Opens a project data file from S3 (via the virtual filesystem).
        """
        try:
            return self.filesystem.open(relative_path, mode, **kwargs)
        except Exception as e:
            self.log.data.error(f"Error opening '{relative_path}': {str(e)}")
            raise

    def exists(self, relative_path: str) -> bool:
        """
        Checks if a file exists in the project data.
        """
        return self.filesystem.exists(relative_path)

    def listdir(self, relative_path: str = "") -> list:
        """
        Lists files in a project data directory.
        """
        try:
            return self.filesystem.listdir(relative_path)
        except Exception as e:
            self.log.data.error(f"Error listing '{relative_path}': {str(e)}")
            raise

    def get_data_path(self, relative_path: str = "") -> str:
        """
        Returns a local filesystem path for a data file.
        Useful for libraries that require a file path instead of a file handle.
        """
        if relative_path:
            return self.filesystem.get_path(relative_path)
        return self.filesystem.temp_dir
