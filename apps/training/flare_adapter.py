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
from typing import Any, Callable, Iterator, Optional
from urllib.parse import urlparse

import boto3
import numpy as np
import nvflare.client as flare
import ray
from botocore.config import Config
from dotenv import load_dotenv

# Load environment variables from a local .env file if it exists.
load_dotenv()


class FlareDataFileSystem:
    """
    A Ray-powered virtual filesystem for NVFlare jobs.

    Provides access to data stored in an S3-compatible service (MinIO). It supports
    standard local downloads for traditional file I/O and Ray Data streaming for
    scalable, parallel preprocessing of large medical datasets.
    """

    def __init__(self, project_uuid: str):
        """
        Initialize the filesystem and prepare the Ray runtime.
        """
        self.project_uuid = (
            os.getenv("SWARMCLOUD_PROJECT_ID", "").strip() or project_uuid
        )

        # Create a temporary directory for local file fallback.
        self.temp_dir = tempfile.mkdtemp(prefix=f"flare_{project_uuid}_")
        self._downloaded_files = {}

        self.use_local_data = (
            os.getenv("SWARMCLOUD_USE_LOCAL_DATA", "1").strip().lower()
            in {"1", "true", "yes", "on"}
        )

        self.bucket = os.getenv("AWS_STORAGE_BUCKET_NAME", "").strip()
        self.s3_client = None
        self.local_s3_endpoint = (
            os.getenv("SWARMCLOUD_LOCAL_S3_ENDPOINT", "").strip()
            or os.getenv("AWS_S3_ENDPOINT_URL", "").strip()
            or "http://minio:9000"
        )
        self.local_s3_region = os.getenv("AWS_S3_REGION_NAME", "").strip() or "us-east-1"
        self.http_timeout = float(
            os.getenv("SWARMCLOUD_HTTP_TIMEOUT", "20").strip() or "20"
        )
        self.s3_connect_timeout = float(
            os.getenv("SWARMCLOUD_S3_CONNECT_TIMEOUT", "3").strip() or "3"
        )
        self.s3_read_timeout = float(
            os.getenv("SWARMCLOUD_S3_READ_TIMEOUT", "20").strip() or "20"
        )

        if self.use_local_data and self.bucket:
            try:
                self.s3_client = self._build_s3_client(
                    endpoint_url=self.local_s3_endpoint or None,
                    region_name=self.local_s3_region or None,
                    addressing_style="path",
                )
            except Exception as e:
                print(f"FlareDataFileSystem: Failed to init local S3 client: {e}")

        # Load the data manifest.
        self.manifest = self._load_manifest()

        # Ray is now lazy-initialized in to_ray_dataset/preprocess to avoid 
        # unnecessary resource overhead and GCS connection issues in simple scripts.

        # Setup SSL context for internal S3 downloads.
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

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
        # IF we are not using local data (i.e. downloading from central server).
        updated_manifest = {}
        for rel_path, url in manifest.items():
            parsed = urlparse(url)
            if self.use_local_data:
                # Force local endpoint (minio:9000) for Ray to stream from
                # We replace the scheme and netloc (host:port)
                new_url = url.replace(f"{parsed.scheme}://{parsed.netloc}", self.local_s3_endpoint.rstrip("/"))
                updated_manifest[rel_path] = new_url
            else:
                # Remote mode: replace localhost/minio with server host
                internal_host = os.getenv("SWARMCLOUD_SERVER_HOST", "").strip()
                if internal_host:
                    new_url = url.replace("localhost", internal_host).replace("127.0.0.1", internal_host).replace("minio", internal_host)
                    updated_manifest[rel_path] = new_url
                else:
                    updated_manifest[rel_path] = url

        return updated_manifest

    def _init_ray(self):
        """Lazy-initialize Ray only when distributed data features are used."""
        if not ray.is_initialized():
            try:
                # Attempt to connect to an existing cluster or start locally.
                ray.init(address="auto", ignore_reinit_error=True)
            except Exception:
                # For local/docker test mode, we use a single-node setup with minimal overhead.
                ray.init(
                    ignore_reinit_error=True, 
                    include_dashboard=False,
                    num_cpus=os.cpu_count() or 2,
                    _system_config={"gcs_rpc_server_reconnect_timeout_s": 60}
                )

    def to_ray_dataset(self) -> ray.data.Dataset:
        """
        Scalable Interface: Converts the manifest into a Ray Dataset.
        This allows for streaming data directly from S3 and parallel preprocessing.
        """
        self._init_ray()
        paths = list(self.manifest.values())
        if not paths:
            raise ValueError("FlareDataFileSystem: Manifest is empty.")

        # Determine format from first file.
        ext = os.path.splitext(paths[0])[1].lower()
        
        # We pass the manifest URLs (presigned) directly to Ray.
        if ext == ".csv":
            return ray.data.read_csv(paths)
        elif ext == ".json":
            return ray.data.read_json(paths)
        else:
            # Default to binary (images/volumes) for medical data.
            return ray.data.read_binary_files(paths)

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
        paths = list(self.manifest.values())
        
        print(f"FlareDataFileSystem: Streaming {len(paths)} files via Ray Data...")
        try:
            # Use binary files to get the raw content of any file type.
            ds = ray.data.read_binary_files(paths, include_paths=True)
            
            # Helper to write to our temp dir
            temp_dir = self.temp_dir
            manifest = self.manifest

            def save_to_disk(row):
                url = row["path"]
                # Match URL back to relative path
                rel_path = next((k for k, v in manifest.items() if v == url), os.path.basename(url))
                local_path = os.path.join(temp_dir, rel_path)
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(row["bytes"])
                return {"rel_path": rel_path, "local_path": local_path}

            # Trigger the Ray execution and collect results
            results = ds.map(save_to_disk).take_all()
            for res in results:
                self._downloaded_files[res["rel_path"]] = res["local_path"]
            
            print(f"FlareDataFileSystem: Successfully streamed {len(results)} files.")
                
        except Exception as e:
            print(f"FlareDataFileSystem: Ray Data streaming failed: {e}. Falling back to serial download.")
            self._download_all_from_manifest_serial()

    def _download_all_from_manifest_serial(self):
        """Serial fallback for downloading files."""
        for rel_path, url in self.manifest.items():
            local_path = os.path.join(self.temp_dir, rel_path)
            if rel_path in self._downloaded_files:
                continue

            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            # 1. Prioritize local S3 download if configured
            downloaded = False
            if self.use_local_data and self.s3_client and self.bucket:
                try:
                    # Construct S3 key: project_uuid/data/rel_path
                    key = f"{self.project_uuid}/data/{rel_path}"
                    self.s3_client.download_file(self.bucket, key, local_path)
                    self._downloaded_files[rel_path] = local_path
                    downloaded = True
                except Exception as e:
                    print(f"FlareDataFileSystem: Local S3 download failed for {rel_path}: {e}. Falling back to URL.")

            # 2. Fallback to URL download via urllib (useful if downloading from server)
            if not downloaded:
                try:
                    opener = urllib.request.build_opener(
                        urllib.request.HTTPSHandler(context=self.ssl_context)
                    )
                    with opener.open(url, timeout=self.http_timeout) as resp, open(local_path, "wb") as f:
                        shutil.copyfileobj(resp, f)
                    self._downloaded_files[rel_path] = local_path
                except Exception as e:
                    print(f"FlareDataFileSystem: Serial download failed for {rel_path}: {e}")


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
            # This happens on the first round if no initial model is provided.
            if (
                input_model.params
                and "numpy_key" in input_model.params
                and len(input_model.params) == 1
            ):
                print(
                    "flare_adapter: Received default dummy model from server. Ignoring parameters for first round."
                )
                input_model.params = {}

            print(
                "flare_adapter: Global model received for "
                f"round {input_model.current_round}."
            )
            return input_model

        print("flare_adapter: No model received. Training is likely complete.")
        return None
    except Exception as e:
        print(f"flare_adapter: Exception during model reception: {e}")
        return None


def send_model(params, metrics: dict = None, meta: dict = None):
    """
    Sends the locally updated model weights and optional metrics back to the server.

    Args:
        params (dict): Model weights as a dictionary of numpy arrays.
        metrics (dict, optional): Accuracy, loss, or other performance indicators.
        meta (dict, optional): Metadata such as NUM_STEPS_CURRENT_ROUND.
    """
    print("flare_adapter: Sending updated model to server...")

    if params is None:
        raise ValueError(
            "flare_adapter.send_model received params=None. "
            "Training must send model weights (e.g. model.state_dict() for PyTorch, model.get_weights() for Keras)."
        )

    # Accept common model-weight structures and normalize to dict[str, value].
    if isinstance(params, (list, tuple)):
        params = {str(i): v for i, v in enumerate(params)}
    elif not isinstance(params, dict):
        raise TypeError(
            f"flare_adapter.send_model expects dict/list/tuple for params, got {type(params)}"
        )

    if len(params) == 0:
        raise ValueError(
            "flare_adapter.send_model received empty params. "
            "No model weights were produced by training."
        )

    # Ensure all data types (like PyTorch tensors) are converted to numpy for transport.
    params = _ensure_transportable(params)

    if len(params) == 0:
        raise ValueError(
            "flare_adapter.send_model converted params are empty; cannot submit update without weights."
        )

    # Ensure NUM_STEPS_CURRENT_ROUND is present in meta for aggregator weighting.
    if meta is None:
        meta = {}
    
    # InTimeAccumulateWeightedAggregator specifically looks for 'aggregation_weight'
    # We map NUM_STEPS_CURRENT_ROUND to it for compatibility.
    if "NUM_STEPS_CURRENT_ROUND" in meta:
        meta["aggregation_weight"] = meta["NUM_STEPS_CURRENT_ROUND"]
    elif "aggregation_weight" not in meta:
        meta["NUM_STEPS_CURRENT_ROUND"] = 1
        meta["aggregation_weight"] = 1.0

    # Standard NVFlare metadata keys
    # NUM_STEPS_CURRENT_ROUND is used by aggregators for weighted averaging.
    output_model = flare.FLModel(
        params=params,
        metrics=metrics if metrics is not None else {},
        meta=meta,
    )
    flare.send(output_model)
    print("flare_adapter: Model successfully sent to server.")


# =================================================================================
# Helper functions for framework-specific conversions
# =================================================================================


def get_weights_list(params: dict):
    """
    Converts a dictionary of parameters back to a sorted list of numpy arrays.
    Required for Keras/TensorFlow's model.set_weights() method.
    """
    try:
        # Sort by key (assuming keys are string indices "0", "1", "2"...)
        sorted_keys = sorted(params.keys(), key=lambda x: int(x))
        return [np.array(params[k]) for k in sorted_keys]
    except (ValueError, AttributeError):
        # Fallback to simple sorted values if keys aren't numeric.
        return [np.array(v) for k, v in sorted(params.items())]


def get_pytorch_state_dict(params: dict):
    """
    Converts a dictionary of numpy arrays to PyTorch tensors.
    Required for PyTorch's model.load_state_dict() method.
    """
    import torch

    return {k: torch.as_tensor(v) for k, v in params.items()}


def _ensure_transportable(params: dict):
    """
    Utility to ensure all parameters are converted to numpy arrays
    before being sent over the network by NVFlare.
    """
    converted = {}
    for k, v in params.items():
        # Keep torch tensors as tensors for PTInProcessClientAPIExecutor,
        # which expects tensor params and handles conversion internally.
        if hasattr(v, "detach") and hasattr(v, "cpu"):
            converted[k] = v.detach().cpu()
        elif hasattr(v, "numpy"):  # Handle TensorFlow/Keras tensors
            converted[k] = v.numpy()
        elif isinstance(v, (str, bytes, bytearray)):
            # Pass JSON strings / raw bytes through unchanged so that
            # aggregators can call json.loads()
            # on them without receiving a numpy array.
            converted[k] = v
        else:
            converted[k] = np.array(v)
    return converted
