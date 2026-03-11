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
            or ""
        )
        self.local_s3_region = os.getenv("AWS_S3_REGION_NAME", "").strip()
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
                print(f"FlareDataFileSystem: Failed to init local S3: {e}")

        # Load the data manifest.
        self.manifest = self._load_manifest()

        # Initialize Ray for scalable preprocessing.
        if not ray.is_initialized():
            try:
                # Attempt to connect to an existing cluster or start locally.
                ray.init(address="auto", ignore_reinit_error=True)
            except Exception:
                ray.init(ignore_reinit_error=True)

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
        for loc in manifest_locations:
            if os.path.exists(loc):
                try:
                    with open(loc) as f:
                        return json.load(f)
                except Exception:
                    continue
        return {}

    def to_ray_dataset(self) -> ray.data.Dataset:
        """
        Scalable Interface: Converts the manifest into a Ray Dataset.
        This allows for streaming data directly from S3 and parallel preprocessing.
        """
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
        return ds.map(fn)

    def get_data_path(self) -> str:
        """
        Legacy Interface: Downloads all project data locally and returns the root path.
        Recommended only for small datasets or legacy training scripts.
        """
        self._download_all_from_manifest()
        return self.temp_dir

    def _build_s3_client(self, endpoint_url, region_name, addressing_style):
        return boto3.client(
            "s3",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=region_name,
            endpoint_url=endpoint_url,
            verify=False,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": addressing_style},
                connect_timeout=self.s3_connect_timeout,
                read_timeout=self.s3_read_timeout,
                retries={"max_attempts": 2},
            ),
        )

    def _download_all_from_manifest(self):
        if not self.manifest:
            return

        for rel_path, url in self.manifest.items():
            local_path = os.path.join(self.temp_dir, rel_path)
            if local_path not in self._downloaded_files.values():
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                try:
                    opener = urllib.request.build_opener(
                        urllib.request.HTTPSHandler(context=self.ssl_context)
                    )
                    with opener.open(url, timeout=self.http_timeout) as resp, open(local_path, "wb") as f:
                        shutil.copyfileobj(resp, f)
                    self._downloaded_files[rel_path] = local_path
                except Exception as e:
                    print(f"FlareDataFileSystem: Download failed for {rel_path}: {e}")

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


def send_model(params, metrics: dict = None):
    """
    Sends the locally updated model weights and optional metrics back to the server.

    Args:
        params (dict): Model weights as a dictionary of numpy arrays.
        metrics (dict, optional): Accuracy, loss, or other performance indicators.
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

    output_model = flare.FLModel(
        params=params,
        metrics=metrics if metrics is not None else {},
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
        else:
            converted[k] = np.array(v)
    return converted
