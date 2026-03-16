"""
Visualization context and utilities for the results application.
Provides the bridge between user-written Python scripts and the project's
stored data and model weights.
"""

import base64
import io
import os
import socket
from urllib.parse import urlparse

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import torch
import fsspec
from data.filesystem import DataFileSystem
from logs import logger
from training.models import TrainingJob

# Use a non-interactive backend for Matplotlib to work in background tasks.
matplotlib.use("Agg")
# Save text in SVGs as text objects rather than paths for better accessibility.
matplotlib.rcParams["svg.fonttype"] = "none"


class ResultsVisualizationContext:
    """
    Context manager that provides a safe and easy-to-use API for
    user-submitted visualization scripts.
    """

    def __init__(self, project_uuid: str, job_identifier: str, run_id: str):
        self.project_uuid = project_uuid
        self.job_identifier = job_identifier
        self.run_id = run_id
        self.filesystem = DataFileSystem(project_uuid)
        self.plots = []
        self.current_plot_number = 0
        self.log = logger.get_logger()
        # Initialize internal streaming filesystem.
        # Request-level SSL verification is handled in self.open() and self.get_model().
        self.fs = fsspec.filesystem("http")

        try:
            self.job = TrainingJob.objects.get(identifier=job_identifier)
        except (TrainingJob.DoesNotExist, ValueError):
            self.job = TrainingJob.objects.filter(
                flare_job_id__icontains=job_identifier
            ).first()

    def __enter__(self):
        self.log.results.info(f"Entering ResultsVisualizationContext for project={self.project_uuid}")
        self.filesystem.__enter__()
        if not self.filesystem.manifest:
            self.filesystem.build_manifest()
        
        # Post-process manifest for internal reachability (e.g. from sandbox)
        self.filesystem.manifest = self._process_manifest(self.filesystem.manifest)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.filesystem.__exit__(exc_type, exc_val, exc_tb)

    def _process_manifest(self, manifest: dict) -> dict:
        """Ensures that URLs in the manifest are reachable from the current environment."""
        internal_host = "minio"
        try:
            socket.gethostbyname("minio")
        except socket.gaierror:
            internal_host = "host.docker.internal"

        updated = {}
        for rel_path, url in manifest.items():
            p = urlparse(url)
            # If the hostname is a Tailscale IP or localhost, replace it with 'minio'
            # which is reachable inside the Docker network.
            if p.hostname != internal_host:
                new_url = url.replace(p.netloc, f"{internal_host}:{p.port or 9000}")
            else:
                new_url = url
            updated[rel_path] = new_url
        return updated

    def get_model(self, client_name="fl-client-1", model_filename="model.pt"):
        """Loads and returns model weights using streaming fsspec."""
        flare_id = self.job_identifier
        if self.job:
            flare_id = self.job.flare_job_id # Use raw ID for prefix matching
            
        # Try to find the model in the manifest
        # The manifest is built by filesystem.build_manifest()
        candidates = [
            f"results/{client_name}/{model_filename}",
            f"{self.project_uuid}/results/{flare_id}/{client_name}/{model_filename}",
        ]
        
        url = None
        for cand in candidates:
            if cand in self.filesystem.manifest:
                url = self.filesystem.manifest[cand]
                break
        
        if not url:
            # Fallback scan of manifest for any model file
            for k, v in self.filesystem.manifest.items():
                if "results/" in k and k.endswith((".pt", ".npy", ".npz")):
                    url = v
                    break
        
        if not url:
            raise FileNotFoundError(f"Could not find model weights in manifest.")

        # We pass ssl=False to individual requests to support internal MinIO.
        with self.fs.open(url, "rb", ssl=False) as f:
            if url.endswith(".pt"):
                data = torch.load(f, map_location="cpu", weights_only=True)
                if isinstance(data, dict):
                    return data.get("numpy_key", data.get("weights", data.get("model", data)))
                return data
            elif url.endswith(".npy"):
                return np.load(f, allow_pickle=False)
            elif url.endswith(".npz"):
                d = np.load(f, allow_pickle=False)
                return d.get("params", d.get("weights", d))
        return None

    def open(self, relative_path: str, mode: str = "r", **kwargs):
        """Opens a file from the manifest via streaming."""
        clean_path = relative_path.lstrip("/")
        if clean_path not in self.filesystem.manifest:
            raise FileNotFoundError(f"File {clean_path} not found in manifest.")
        # We pass ssl=False to individual requests to support internal MinIO.
        kwargs.setdefault("ssl", False)
        return self.fs.open(self.filesystem.manifest[clean_path], mode=mode, **kwargs)

    def exists(self, relative_path: str) -> bool:
        return relative_path.lstrip("/") in self.filesystem.manifest

    def listdir(self, relative_path: str = "") -> list:
        return self.filesystem.listdir(relative_path)

    def get_data_path(self, relative_path: str = "") -> str:
        """Returns the internal URL for streaming."""
        if not relative_path: return self.filesystem.temp_dir
        return self.filesystem.manifest.get(relative_path.lstrip("/"), "")
