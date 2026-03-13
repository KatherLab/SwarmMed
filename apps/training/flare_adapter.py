"""
NVIDIA FLARE Adapter for SwarmCloud.
This module provides a simplified interface for training scripts to interact
with the NVFlare system and the project's S3 data storage.
"""

import json
import os
import shutil
import ssl
import tempfile
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse

import numpy as np
import nvflare.client as flare
import ray
from dotenv import load_dotenv

# Load environment variables from a local .env file if it exists.
load_dotenv()


class FlareDataFileSystem:
    """
    A Ray-powered virtual filesystem for NVFlare jobs.

    Provides access to data stored in an S3-compatible service (MinIO). It strictly
    uses Ray Data streaming for scalable, parallel data access.
    """

    def __init__(self, project_uuid: str):
        """
        Initialize the filesystem.
        """
        self.project_uuid = (
            os.getenv("SWARMCLOUD_PROJECT_ID", "").strip() or project_uuid
        )

        # Create a temporary directory for local file fallback.
        self.temp_dir = tempfile.mkdtemp(prefix=f"flare_{self.project_uuid}_")
        self._downloaded_files = {}

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
        
        # Post-process URLs to ensure they are reachable.
        updated_manifest = {}
        for rel_path, url in manifest.items():
            parsed = urlparse(url)
            if self.use_local_data:
                # Force local endpoint. 
                # We normalize to HTTP for local mode to simplify Ray/urllib connectivity.
                endpoint = self.local_s3_endpoint.replace("https://", "http://")
                new_url = url.replace(f"{parsed.scheme}://{parsed.netloc}", endpoint.rstrip("/"))
                updated_manifest[rel_path] = new_url
            else:
                # Remote mode: replace localhost/minio with server host if provided.
                internal_host = os.getenv("SWARMCLOUD_SERVER_HOST", "").strip()
                if internal_host:
                    new_url = url.replace("localhost", internal_host).replace("127.0.0.1", internal_host).replace("minio", internal_host)
                    updated_manifest[rel_path] = new_url
                else:
                    updated_manifest[rel_path] = url

        return updated_manifest

    def _init_ray(self):
        """Lazy-initialize Ray with robust settings for Docker environments."""
        if not ray.is_initialized():
            try:
                # Attempt to connect to an existing cluster or start locally.
                ray.init(address="auto", ignore_reinit_error=True)
            except Exception:
                # Fallback to a lightweight local instance.
                ray.init(
                    ignore_reinit_error=True, 
                    include_dashboard=False,
                    num_cpus=1,
                    _node_ip_address="127.0.0.1",
                )

    def to_ray_dataset(self) -> ray.data.Dataset:
        """
        Scalable Interface: Converts the manifest into a Ray Dataset.
        Uses parallel urllib streaming for maximum reliability with internal endpoints and SSL.
        """
        self._init_ray()
        items = list(self.manifest.items())
        if not items:
            raise ValueError("FlareDataFileSystem: Manifest is empty.")

        # Construct base dataset from manifest entries
        ds = ray.data.from_items([{"rel_path": k, "url": v} for k, v in items])
        
        # SSL context for urllib (ignoring self-signed certs)
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        def stream_data(row):
            url = row["url"]
            with urllib.request.urlopen(url, context=ssl_ctx) as response:
                row["bytes"] = response.read()
            return row

        # Parallel download into Ray memory
        binary_ds = ds.map(stream_data)
        
        # Determine format from first file extension.
        ext = os.path.splitext(items[0][0])[1].lower()
        
        if ext == ".csv":
            import pandas as pd
            import io
            return binary_ds.map_batches(lambda batch: pd.read_csv(io.BytesIO(batch["bytes"][0])))
        elif ext == ".json":
            return binary_ds.map_batches(lambda batch: json.loads(batch["bytes"][0]))
        else:
            return binary_ds

    def preprocess(self, ds: ray.data.Dataset, fn: Callable[[Any], Any]) -> ray.data.Dataset:
        """
        Applies a parallel preprocessing function across the Ray dataset.
        """
        self._init_ray()
        return ds.map(fn)

    def get_data_path(self) -> str:
        """
        Legacy Interface: Streams all project data to the local filesystem using Ray Data.
        """
        self._download_all_via_ray()
        return self.temp_dir

    def _download_all_via_ray(self):
        """Uses Ray Data to stream all project files to the local temp directory."""
        if not self.manifest:
            return

        self._init_ray()
        items = list(self.manifest.items())
        
        print(f"FlareDataFileSystem: Parallel streaming {len(items)} files via Ray Data...")
        
        # Construct base dataset
        ds = ray.data.from_items([{"rel_path": k, "url": v} for k, v in items])
        
        temp_dir = self.temp_dir
        # SSL context for urllib
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        def download_file(row):
            rel_path = row["rel_path"]
            url = row["url"]
            local_path = os.path.join(temp_dir, rel_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            # Use urllib for proven reliability with SSL/proxies in this project
            with urllib.request.urlopen(url, context=ssl_ctx) as response, open(local_path, "wb") as f:
                shutil.copyfileobj(response, f)
                
            row["local_path"] = local_path
            return row

        # Trigger parallel execution
        results = ds.map(download_file).take_all()
        for res in results:
            self._downloaded_files[res["rel_path"]] = res["local_path"]
        
        print(f"FlareDataFileSystem: Successfully streamed {len(results)} files.")

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
    """
    Initializes the NVFlare client.
    Must be called at the very beginning of training.py.
    """
    flare.init()
    print("flare_adapter: NVIDIA FLARE client initialized.")


def get_data_filesystem(project_id: str) -> FlareDataFileSystem:
    """
    Factory function to create a FlareDataFileSystem instance.
    """
    return FlareDataFileSystem(project_uuid=project_id)


def receive_model():
    """
    Receives the latest global model (aggregated weights) from the server.
    """
    print("flare_adapter: Receiving global model from server...")
    try:
        input_model = flare.receive()
        if input_model:
            if (
                input_model.params
                and "numpy_key" in input_model.params
                and len(input_model.params) == 1
            ):
                input_model.params = {}
            return input_model
        return None
    except Exception as e:
        print(f"flare_adapter: Exception during model reception: {e}")
        return None


def send_model(params, metrics: dict = None, meta: dict = None):
    """
    Sends the locally updated model weights and optional metrics back to the server.
    """
    print("flare_adapter: Sending updated model to server...")

    if params is None:
        raise ValueError("flare_adapter.send_model received params=None.")

    if isinstance(params, (list, tuple)):
        params = {str(i): v for i, v in enumerate(params)}
    elif not isinstance(params, dict):
        raise TypeError(f"flare_adapter.send_model expects dict/list/tuple for params, got {type(params)}")

    params = _ensure_transportable(params)

    if meta is None:
        meta = {}
    
    if "NUM_STEPS_CURRENT_ROUND" in meta:
        meta["aggregation_weight"] = meta["NUM_STEPS_CURRENT_ROUND"]
    elif "aggregation_weight" not in meta:
        meta["NUM_STEPS_CURRENT_ROUND"] = 1
        meta["aggregation_weight"] = 1.0

    output_model = flare.FLModel(
        params=params,
        metrics=metrics if metrics is not None else {},
        meta=meta,
    )
    flare.send(output_model)
    print("flare_adapter: Model successfully sent to server.")


def get_weights_list(params: dict):
    """Required for Keras/TensorFlow's model.set_weights() method."""
    try:
        sorted_keys = sorted(params.keys(), key=lambda x: int(x))
        return [np.array(params[k]) for k in sorted_keys]
    except (ValueError, AttributeError):
        return [np.array(v) for k, v in sorted(params.items())]


def get_pytorch_state_dict(params: dict):
    """Required for PyTorch's model.load_state_dict() method."""
    import torch
    return {k: torch.as_tensor(v) for k, v in params.items()}


def _ensure_transportable(params: dict):
    """Utility to ensure all parameters are converted to numpy arrays."""
    converted = {}
    for k, v in params.items():
        if hasattr(v, "detach") and hasattr(v, "cpu"):
            converted[k] = v.detach().cpu().numpy()
        elif hasattr(v, "numpy"):
            converted[k] = v.numpy()
        elif isinstance(v, (str, bytes, bytearray)):
            converted[k] = v
        else:
            converted[k] = np.array(v)
    return converted
