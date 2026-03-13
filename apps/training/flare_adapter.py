"""
NVIDIA FLARE Adapter for SwarmCloud.
This module provides a simplified, streaming interface for training scripts to
interact with the NVFlare system and the project's S3 data storage.
"""

import json
import os
import shutil
import ssl
import tempfile
from typing import Any, List, Optional
from urllib.parse import urlparse

import numpy as np
import nvflare.client as flare
import fsspec
from dotenv import load_dotenv

# Load environment variables from a local .env file if it exists.
load_dotenv()


class FlareDataFileSystem:
    """
    A streaming virtual filesystem for NVFlare jobs powered by fsspec.
    Provides on-demand access to data stored in MinIO via presigned URLs.
    
    Training scripts can use this like a standard filesystem:
    >>> with flare_adapter.get_data_filesystem(project_id) as fs:
    >>>     with fs.open("data.csv") as f:
    >>>         df = pd.read_csv(f)
    """

    def __init__(self, project_uuid: str):
        self.project_uuid = (
            os.getenv("SWARMCLOUD_PROJECT_ID", "").strip() or project_uuid
        )
        self.use_local_data = (
            os.getenv("SWARMCLOUD_USE_LOCAL_DATA", "1").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.local_s3_endpoint = (
            os.getenv("SWARMCLOUD_LOCAL_S3_ENDPOINT", "").strip()
            or os.getenv("AWS_S3_ENDPOINT_URL", "").strip()
            or "http://minio:9000"
        )

        # Load and process the data manifest.
        self.manifest = self._load_manifest()
        
        # Initialize fsspec HTTP filesystem for streaming.
        self.fs = fsspec.filesystem("http")
        
        # Create a temporary directory only for legacy compatibility or if explicitly needed.
        self.temp_dir = tempfile.mkdtemp(prefix=f"flare_{self.project_uuid}_")

    def _load_manifest(self) -> dict:
        manifest_locations = [
            os.path.join(os.getcwd(), "data_manifest.json"),
            os.path.join(os.path.dirname(__file__), "data_manifest.json"),
            os.path.join(os.getcwd(), "custom", "data_manifest.json"),
        ]
        manifest = {}
        for loc in manifest_locations:
            if os.path.exists(loc):
                try:
                    with open(loc) as f:
                        manifest = json.load(f)
                        break
                except Exception:
                    continue
        
        # Post-process URLs to ensure they are reachable from inside the container.
        updated_manifest = {}
        internal_host = os.getenv("SWARMCLOUD_SERVER_HOST", "").strip()
        
        for rel_path, url in manifest.items():
            parsed = urlparse(url)
            if self.use_local_data:
                endpoint = self.local_s3_endpoint
                # Replace the whole scheme and netloc with the provided endpoint
                new_url = url.replace(f"{parsed.scheme}://{parsed.netloc}", endpoint.rstrip("/"))
                updated_manifest[rel_path] = new_url
            else:
                if internal_host:
                    new_url = url.replace("localhost", internal_host).replace("127.0.0.1", internal_host).replace("minio", internal_host)
                    updated_manifest[rel_path] = new_url
                else:
                    updated_manifest[rel_path] = url
        return updated_manifest

    def ls(self, path: str = "") -> List[str]:
        """Lists available files in the virtual filesystem."""
        path = path.strip("/")
        if not path:
            return list(self.manifest.keys())
        # Return unique top-level entries under the path
        results = set()
        for p in self.manifest.keys():
            if p.startswith(path):
                rel = p[len(path):].lstrip("/")
                part = rel.split("/")[0]
                if part:
                    results.add(part)
        return sorted(list(results))

    def glob(self, pattern: str) -> List[str]:
        """Simple glob matching for manifest paths."""
        import fnmatch
        return [p for p in self.manifest.keys() if fnmatch.fnmatch(p, pattern)]

    def exists(self, path: str) -> bool:
        """Checks if a path exists in the manifest."""
        path = path.lstrip("/")
        if path in self.manifest:
            return True
        # Check if it's a "directory" prefix
        prefix = path.rstrip("/") + "/"
        return any(k.startswith(prefix) for k in self.manifest.keys())

    def isfile(self, path: str) -> bool:
        """Checks if path is a file."""
        return path.lstrip("/") in self.manifest

    def isdir(self, path: str) -> bool:
        """Checks if path is a directory prefix."""
        prefix = path.lstrip("/").rstrip("/") + "/"
        return any(k.startswith(prefix) for k in self.manifest.keys())

    def open(self, path: str, mode: str = "rb", **kwargs):
        """
        Streams a file from the manifest.
        Returns a file-like object that can be passed to pandas, torch, etc.
        """
        clean_path = path.lstrip("/")
        if clean_path not in self.manifest:
            raise FileNotFoundError(f"File not found in manifest: {path}")
        
        url = self.manifest[clean_path]
        return self.fs.open(url, mode=mode, **kwargs)

    def read_bytes(self, path: str) -> bytes:
        """Reads all bytes from a file."""
        with self.open(path, "rb") as f:
            return f.read()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Reads all text from a file."""
        with self.open(path, "r", encoding=encoding) as f:
            return f.read()

    def __getitem__(self, path: str):
        """Allows fs['path'] access."""
        return self.read_bytes(path)

    def get_data_path(self) -> str:
        """
        Legacy Interface: Materializes all data to a local temp directory.
        Prefer using fs.open() for efficient streaming.
        """
        print("flare_adapter: Materializing data to local temp directory (Legacy Mode)...")
        for rel_path, url in self.manifest.items():
            local_path = os.path.join(self.temp_dir, rel_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with self.fs.open(url) as remote_f, open(local_path, "wb") as local_f:
                shutil.copyfileobj(remote_f, local_f)
        return self.temp_dir

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()

    def cleanup(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)


# =================================================================================
# Public Adapter Functions
# =================================================================================


def init_flare():
    """Initializes the NVFlare client."""
    flare.init()
    print("flare_adapter: NVIDIA FLARE client initialized.")


def get_data_filesystem(project_id: str) -> FlareDataFileSystem:
    """Factory function to create a FlareDataFileSystem instance."""
    return FlareDataFileSystem(project_uuid=project_id)


def receive_model():
    """Receives the latest global model from the server."""
    print("flare_adapter: Receiving global model...")
    try:
        input_model = flare.receive()
        if input_model and input_model.params:
            # Handle default dummy model from some NVFlare versions
            if "numpy_key" in input_model.params and len(input_model.params) == 1:
                input_model.params = {}
        return input_model
    except Exception as e:
        print(f"flare_adapter: Error receiving model: {e}")
        return None


def send_model(params, metrics: dict = None, meta: dict = None):
    """Sends model updates and metrics back to the server."""
    if params is None:
        raise ValueError("flare_adapter: send_model received params=None.")

    # Convert lists/tuples (common in Keras) to dict
    if isinstance(params, (list, tuple)):
        params = {str(i): v for i, v in enumerate(params)}
    
    params = _ensure_transportable(params)
    
    meta = meta or {}
    if "NUM_STEPS_CURRENT_ROUND" in meta:
        meta["aggregation_weight"] = meta["NUM_STEPS_CURRENT_ROUND"]
    elif "aggregation_weight" not in meta:
        meta["aggregation_weight"] = 1.0

    output_model = flare.FLModel(
        params=params,
        metrics=metrics or {},
        meta=meta,
    )
    flare.send(output_model)
    print("flare_adapter: Model sent to server.")


def get_weights_list(params: dict):
    """Helper for Keras model.set_weights()."""
    try:
        keys = sorted(params.keys(), key=lambda x: int(x))
        return [np.array(params[k]) for k in keys]
    except Exception:
        return [np.array(v) for k, v in sorted(params.items())]


def get_pytorch_state_dict(params: dict):
    """Helper for PyTorch model.load_state_dict()."""
    import torch
    return {k: torch.as_tensor(v) for k, v in params.items()}


def _ensure_transportable(params: dict):
    """Converts tensors and other formats to numpy for transport."""
    converted = {}
    for k, v in params.items():
        if hasattr(v, "detach") and hasattr(v, "cpu"):
            converted[k] = v.detach().cpu().numpy()
        elif hasattr(v, "numpy"):
            converted[k] = v.numpy()
        else:
            converted[k] = np.array(v)
    return converted
