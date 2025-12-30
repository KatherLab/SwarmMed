import os
import tempfile
import shutil
import boto3
import nvflare.client as flare
import numpy as np

class FlareDataFileSystem:
    """
    A self-contained virtual filesystem for NVFlare jobs.

    Provides a file-like interface to data stored in an S3-compatible service (MinIO).
    It works by creating a temporary local directory and downloading files from S3
    on-demand, allowing training scripts to use standard file I/O operations.
    """
    
    def __init__(self, project_uuid: str):
        self.project_uuid = project_uuid
        self.root_prefix = f"{project_uuid}/data/"
        self.temp_dir = tempfile.mkdtemp(prefix=f"flare_{project_uuid}_")
        self._downloaded_files = {}
        self._s3_client = self._create_s3_client()
        self.bucket_name = os.getenv('AWS_STORAGE_BUCKET_NAME')
        print(f"FlareDataFileSystem: Initialized. Temp dir: {self.temp_dir}")

    def _create_s3_client(self):
        """Initializes and returns a boto3 S3 client."""
        try:
            s3_endpoint = os.getenv('AWS_S3_ENDPOINT_URL')
            if s3_endpoint and 'minio' in s3_endpoint:
                # Use internal docker network address for MinIO when running in NVFlare container
                s3_endpoint = 'http://host.docker.internal:9000'
                print(f"FlareDataFileSystem: Overriding S3 endpoint to {s3_endpoint}")

            s3_access_key = os.getenv('AWS_ACCESS_KEY_ID')
            s3_secret_key = os.getenv('AWS_SECRET_ACCESS_KEY')

            if not all([s3_endpoint, s3_access_key, s3_secret_key]):
                raise ValueError("One or more S3 environment variables are not set for MinIO connection.")

            return boto3.client(
                's3',
                endpoint_url=s3_endpoint,
                aws_access_key_id=s3_access_key,
                aws_secret_access_key=s3_secret_key
            )
        except Exception as e:
            print(f"FlareDataFileSystem: ERROR - Failed to create S3 client: {e}")
            raise

    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            print(f"FlareDataFileSystem: Exiting context with error: {exc_val}")
        self.cleanup()

    def cleanup(self):
        """Cleans up the temporary directory."""
        print(f"FlareDataFileSystem: Cleaning up temporary directory {self.temp_dir}")
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _download_s3_path(self, s3_prefix: str):
        """Downloads all files from a given S3 prefix to the temp directory."""
        paginator = self._s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=self.bucket_name, Prefix=s3_prefix)

        file_count = 0
        for page in pages:
            if 'Contents' not in page:
                continue
            for obj in page['Contents']:
                key = obj['Key']
                if not key.endswith('/'):
                    # Create a local path that mirrors the S3 structure
                    relative_path = os.path.relpath(key, self.root_prefix)
                    local_path = os.path.join(self.temp_dir, relative_path)
                    
                    if local_path not in self._downloaded_files.values():
                        os.makedirs(os.path.dirname(local_path), exist_ok=True)
                        print(f"FlareDataFileSystem: Downloading {key} to {local_path}...")
                        self._s3_client.download_file(self.bucket_name, key, local_path)
                        self._downloaded_files[relative_path] = local_path
                        file_count += 1
        
        if file_count == 0:
            print(f"FlareDataFileSystem: WARNING - No files found in bucket '{self.bucket_name}' with prefix '{s3_prefix}'.")
        else:
            print(f"FlareDataFileSystem: Successfully downloaded {file_count} files.")

    def get_data_path(self) -> str:
        """
        Ensures all data for the project is downloaded and returns the root
        local path to this data.
        """
        self._download_s3_path(self.root_prefix)
        return self.temp_dir

# =================================================================================
# Public Adapter Functions
# =================================================================================

def init_flare():
    """
    Initializes the FLARE client and prints a confirmation message.
    Should be called at the start of the training script.
    """
    flare.init()
    print("flare_adapter: NVIDIA FLARE client initialized.")

def get_data_filesystem(project_id: str) -> FlareDataFileSystem:
    """
    Returns a FlareDataFileSystem instance for the given project.
    This provides a virtual filesystem that lazily downloads data from S3.
    """
    return FlareDataFileSystem(project_uuid=project_id)

def receive_model():
    """
    Receives the global model from the FLARE server.
    
    Returns:
        flare.FLModel: The received model object, or None if training is finished.
        The 'params' attribute contains the global weights.
    """
    print("flare_adapter: Receiving global model from server...")
    try:
        input_model = flare.receive()
        if input_model:
            print(f"flare_adapter: Global model received for round {input_model.current_round}.")
            return input_model
        else:
            print("flare_adapter: No model received from server. This usually means training is complete.")
            return None
    except Exception as e:
        print(f"flare_adapter: Exception during model reception: {e}")
        return None

def send_model(params: dict, metrics: dict = None):
    """
    Packages the model parameters and metrics and sends them to the server.
    
    Args:
        params (dict): The model parameters (weights). 
            - PyTorch: model.state_dict()
            - TensorFlow/Keras: {str(i): w for i, w in enumerate(model.get_weights())}
            - Scikit-learn: {"coef": model.coef_, "intercept": model.intercept_}
        metrics (dict, optional): Training metrics like {"loss": 0.5}.
    """
    print("flare_adapter: Sending updated model to server...")
    # Ensure all values are correctly formatted for transport
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
    Convert a dictionary of parameters back to a sorted list of numpy arrays.
    Useful for Keras/TensorFlow model.set_weights().
    """
    # Assuming keys are string indices "0", "1", "2"...
    try:
        sorted_keys = sorted(params.keys(), key=lambda x: int(x))
        return [np.array(params[k]) for k in sorted_keys]
    except (ValueError, AttributeError):
        # Fallback if keys are not integers
        return [np.array(v) for k, v in sorted(params.items())]

def get_pytorch_state_dict(params: dict):
    """
    Convert a dictionary of parameters (which may be numpy arrays) to PyTorch tensors.
    Useful for PyTorch model.load_state_dict().
    """
    import torch
    return {k: torch.as_tensor(v) for k, v in params.items()}

def _ensure_transportable(params: dict):
    """
    Utility to ensure all parameters are in a format NVFlare can handle.
    Converts tensors to numpy arrays if needed.
    """
    converted = {}
    for k, v in params.items():
        if hasattr(v, "cpu"): # PyTorch tensor
            converted[k] = v.cpu().numpy()
        elif hasattr(v, "numpy"): # TensorFlow/Keras tensor
            converted[k] = v.numpy()
        else:
            converted[k] = np.array(v)
    return converted