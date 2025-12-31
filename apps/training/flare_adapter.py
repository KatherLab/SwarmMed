"""
NVIDIA FLARE Adapter for SwarmCloud.
This module provides a simplified interface for training scripts to interact
with the NVFlare system and the project's S3 data storage.
"""

import os
import shutil
import tempfile

import boto3
import numpy as np
import nvflare.client as flare
from dotenv import load_dotenv

# Load environment variables from a local .env file if it exists.
# This is crucial for NVFlare workers to get S3 credentials.
load_dotenv()


class FlareDataFileSystem:
    """
    A self-contained virtual filesystem for NVFlare jobs.

    Provides a file-like interface to data stored in an S3-compatible service (MinIO).
    It works by creating a temporary local directory and downloading files from S3
    on-demand, allowing training scripts to use standard file I/O operations.
    """

    def __init__(self, project_uuid: str):
        """
        Initialize the filesystem with the project's unique identifier.
        """
        self.project_uuid = project_uuid
        # Standard prefix where data is stored in the S3 bucket.
        self.root_prefix = f"{project_uuid}/data/"

        # Create a temporary directory on the local machine to store downloaded
        # files.
        self.temp_dir = tempfile.mkdtemp(prefix=f"flare_{project_uuid}_")

        # Track which files have already been downloaded to avoid redundant
        # network calls.
        self._downloaded_files = {}

        # Initialize the S3 client using environment variables.
        self._s3_client = self._create_s3_client()
        self.bucket_name = os.getenv('AWS_STORAGE_BUCKET_NAME')

        if not self.bucket_name:
            print("FlareDataFileSystem: WARNING - AWS_STORAGE_BUCKET_NAME is not set.")

        print(f"FlareDataFileSystem: Initialized. Temp dir: {self.temp_dir}")

    def _create_s3_client(self):
        """
        Initializes and returns a boto3 S3 client with appropriate endpoint overrides.
        """
        try:
            s3_endpoint = os.getenv('AWS_S3_ENDPOINT_URL')
            s3_access_key = os.getenv('AWS_ACCESS_KEY_ID')
            s3_secret_key = os.getenv('AWS_SECRET_ACCESS_KEY')

            if not all([s3_endpoint, s3_access_key, s3_secret_key]):
                raise ValueError(
                    "Missing S3 environment variables. Ensure AWS_S3_ENDPOINT_URL, "
                    "AWS_ACCESS_KEY_ID, and AWS_SECRET_ACCESS_KEY are set.")

            # If running inside a Docker container (NVFlare worker), we often need
            # to point to the host's MinIO instance.
            if 'minio' in s3_endpoint:
                s3_endpoint = 'http://host.docker.internal:9000'
                print(
                    f"FlareDataFileSystem: Overriding S3 endpoint to {s3_endpoint}")

            return boto3.client(
                's3',
                endpoint_url=s3_endpoint,
                aws_access_key_id=s3_access_key,
                aws_secret_access_key=s3_secret_key
            )
        except Exception as e:
            print(
                f"FlareDataFileSystem: ERROR - Failed to create S3 client: {e}")
            raise

    def __enter__(self):
        """Support for 'with' statement."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Ensure cleanup of temporary files when the context is closed."""
        if exc_type:
            print(
                f"FlareDataFileSystem: Exiting context with error: {exc_val}")
        self.cleanup()

    def cleanup(self):
        """Deletes the temporary directory and all its contents."""
        print(f"FlareDataFileSystem: Cleaning up directory {self.temp_dir}")
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _download_s3_path(self, s3_prefix: str):
        """
        Recursively downloads all files from a given S3 prefix into the
        local temporary directory, mirroring the remote structure.
        """
        if not self.bucket_name:
            print("FlareDataFileSystem: ERROR - Cannot download without bucket name.")
            return

        paginator = self._s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=self.bucket_name, Prefix=s3_prefix)

        file_count = 0
        for page in pages:
            if 'Contents' not in page:
                continue

            for obj in page['Contents']:
                key = obj['Key']
                # Skip directory markers.
                if not key.endswith('/'):
                    # Convert S3 key to a relative path within our temp
                    # directory.
                    relative_path = os.path.relpath(key, self.root_prefix)
                    local_path = os.path.join(self.temp_dir, relative_path)

                    # Only download if we haven't seen this path yet.
                    if local_path not in self._downloaded_files.values():
                        os.makedirs(os.path.dirname(local_path), exist_ok=True)
                        print(f"FlareDataFileSystem: Downloading {key}...")
                        self._s3_client.download_file(
                            self.bucket_name, key, local_path
                        )
                        self._downloaded_files[relative_path] = local_path
                        file_count += 1

        if file_count == 0:
            print(
                f"FlareDataFileSystem: WARNING - No files found with prefix '{s3_prefix}'"
            )
        else:
            print(
                f"FlareDataFileSystem: Successfully downloaded {file_count} files.")

    def get_data_path(self) -> str:
        """
        Ensures all project data is synced locally and returns the root path.
        User scripts use this path to locate their CSVs, images, etc.
        """
        self._download_s3_path(self.root_prefix)
        return self.temp_dir


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


def send_model(params: dict, metrics: dict = None):
    """
    Sends the locally updated model weights and optional metrics back to the server.

    Args:
        params (dict): Model weights as a dictionary of numpy arrays.
        metrics (dict, optional): Accuracy, loss, or other performance indicators.
    """
    print("flare_adapter: Sending updated model to server...")

    # Ensure all data types (like PyTorch tensors) are converted to numpy for
    # transport.
    params = _ensure_transportable(params)

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
        if hasattr(v, "cpu"):  # Handle PyTorch tensors
            converted[k] = v.cpu().numpy()
        elif hasattr(v, "numpy"):  # Handle TensorFlow/Keras tensors
            converted[k] = v.numpy()
        else:
            # Assume it's already numpy-compatible or needs simple wrapping.
            converted[k] = np.array(v)
    return converted
