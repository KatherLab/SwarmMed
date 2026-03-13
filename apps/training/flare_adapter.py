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
from typing import Any, Callable, Iterator, Optional
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

        self.bucket = os.getenv("AWS_STORAGE_BUCKET_NAME", "").strip() or "swarmcloud"
        self.local_s3_endpoint = (
            os.getenv("SWARMCLOUD_LOCAL_S3_ENDPOINT", "").strip()
            or os.getenv("AWS_S3_ENDPOINT_URL", "").strip()
            or "http://minio:9000"
        )
        
        self.http_timeout = float(
            os.getenv("SWARMCLOUD_HTTP_TIMEOUT", "20").strip() or "20"
        )

        # Load and process the data manifest.
        self.manifest = self._load_manifest()

        # Ray is lazy-initialized in to_ray_dataset/preprocess/get_data_path.

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
        
        # Post-process URLs to ensure they are reachable from remote clients.
        updated_manifest = {}
        for rel_path, url in manifest.items():
            parsed = urlparse(url)
            if self.use_local_data:
                # Local mode: ensure we use the local endpoint.
                new_url = url.replace(f"{parsed.scheme}://{parsed.netloc}", self.local_s3_endpoint.rstrip("/"))
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
                # We use 127.0.0.1 for GCS stability in containers and avoid invalid config parameters.
                try:
                    ray.init(
                        ignore_reinit_error=True, 
                        include_dashboard=False,
                        num_cpus=1,
                        _node_ip_address="127.0.0.1",
                        _system_config={
                            "gcs_rpc_server_reconnect_timeout_s": 60,
                        }
                    )
                except Exception:
                    ray.init(ignore_reinit_error=True)

    def _get_arrow_filesystem(self):
        """Creates a PyArrow S3FileSystem configured for the local MinIO instance."""
        if self.use_local_data:
            try:
                from pyarrow import fs
                # Ensure we handle localhost/127.0.0.1 correctly inside container
                endpoint = self.local_s3_endpoint
                parsed = urlparse(endpoint)
                
                # If we are in host network mode, 127.0.0.1 is fine.
                # If not, and endpoint is 127.0.0.1, it might fail unless we use host.docker.internal.
                # SwarmCloud tasks.py usually sets up host network mode for clients.
                
                return fs.S3FileSystem(
                    access_key=os.getenv("AWS_ACCESS_KEY_ID"),
                    secret_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
                    endpoint_override=parsed.netloc,
                    scheme=parsed.scheme or "http",
                    tls_verify_certificates=False, # Correct parameter for recent pyarrow
                    force_virtual_addressing=False  # Required for MinIO
                )
            except Exception as e:
                print(f"FlareDataFileSystem: Failed to init Arrow S3 filesystem: {e}")
        return None

    def _get_ray_paths(self):
        """Returns s3:// URIs for local mode to bypass URL/SSL issues with Ray Data."""
        if self.use_local_data and self.bucket:
            # We must use the bucket name from environment
            return [f"s3://{self.bucket}/{self.project_uuid}/data/{rel_path}" for rel_path in self.manifest.keys()]
        return list(self.manifest.values())

    def to_ray_dataset(self) -> ray.data.Dataset:
        """
        Scalable Interface: Converts the manifest into a Ray Dataset.
        """
        self._init_ray()
        paths = self._get_ray_paths()
        filesystem = self._get_arrow_filesystem()
        
        if not paths:
            raise ValueError("FlareDataFileSystem: Manifest is empty.")

        # Determine format from first file extension.
        ext = os.path.splitext(list(self.manifest.keys())[0])[1].lower()
        
        if ext == ".csv":
            return ray.data.read_csv(paths, filesystem=filesystem)
        elif ext == ".json":
            return ray.data.read_json(paths, filesystem=filesystem)
        else:
            # Default to binary for medical imaging/volumes.
            return ray.data.read_binary_files(paths, filesystem=filesystem)

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
        paths = self._get_ray_paths()
        filesystem = self._get_arrow_filesystem()
        
        print(f"FlareDataFileSystem: Streaming {len(paths)} files via Ray Data...")
        
        # Read files as binary blobs.
        ds = ray.data.read_binary_files(paths, include_paths=True, filesystem=filesystem)
        
        temp_dir = self.temp_dir
        # Map paths back to relative keys for local storage
        if self.use_local_data and self.bucket:
            uri_to_rel = {f"s3://{self.bucket}/{self.project_uuid}/data/{k}": k for k in self.manifest.keys()}
        else:
            uri_to_rel = {v: k for k, v in self.manifest.items()}

        def save_to_disk(row):
            uri = row["path"]
            rel_path = uri_to_rel.get(uri, os.path.basename(uri))
            local_path = os.path.join(temp_dir, rel_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(row["bytes"])
            return {"rel_path": rel_path, "local_path": local_path}

        # Trigger execution and collect results. 
        results = ds.map(save_to_disk).take_all()
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

    Returns:
        flare.FLModel: The received model object, or None if training is done.
    """
    print("flare_adapter: Receiving global model from server...")
    try:
        input_model = flare.receive()
        if input_model:
            # Check for NVFlare NPModelPersistor default dummy model
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

    # normalization to dict[str, value].
    if isinstance(params, (list, tuple)):
        params = {str(i): v for i, v in enumerate(params)}
    elif not isinstance(params, dict):
        raise TypeError(f"flare_adapter.send_model expects dict/list/tuple for params, got {type(params)}")

    # Ensure all data types (like PyTorch tensors) are converted to numpy for transport.
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
