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

        self._internal_hosts = {
            "minio",
            "localhost",
            "127.0.0.1",
            "host.docker.internal",
        }
        self.local_s3_endpoint = self._normalize_local_endpoint(
            self.local_s3_endpoint
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
        
        # If no internal host is set, try to discover a reachable IP/Hostname.
        if not internal_host:
            import socket
            try:
                # Check if we can resolve minio hostname (standard docker compose)
                socket.gethostbyname("minio")
                internal_host = "minio"
            except socket.gaierror:
                # Check for public_url host as candidate (e.g. Tailscale IP)
                public_url = os.getenv("PUBLIC_URL", "")
                if public_url:
                    p = urlparse(public_url)
                    if p.hostname and p.hostname not in {"localhost", "127.0.0.1"}:
                        internal_host = p.hostname
                
                if not internal_host:
                    # Fallback to macOS special host for Docker
                    internal_host = "host.docker.internal"

        for rel_path, url in manifest.items():
            parsed = urlparse(url)
            # 1. First, handle potential use_local_data override
            if self.use_local_data:
                endpoint = self.local_s3_endpoint.rstrip("/")
                # Replace the whole scheme and netloc with the provided endpoint
                old_base = f"{parsed.scheme}://{parsed.netloc}"
                new_url = url.replace(old_base, endpoint)
            else:
                new_url = url

            # 2. Robustly replace internal host candidates if they persist (127.0.0.1/localhost)
            new_url = new_url.replace("localhost", internal_host)
            new_url = new_url.replace("127.0.0.1", internal_host)
            
            # If the hostname is exactly 'minio', replace it with internal_host if it's different
            parsed_new = urlparse(new_url)
            if parsed_new.hostname == "minio" and internal_host != "minio":
                new_url = new_url.replace("minio", internal_host, 1)
            
            updated_manifest[rel_path] = new_url
            
        return updated_manifest

    def _normalize_local_endpoint(self, endpoint: str) -> str:
        parsed = urlparse(endpoint)
        if (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower() in self._internal_hosts
        ):
            return endpoint.replace("https://", "http://", 1)
        return endpoint

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

        primary_url = self.manifest[clean_path]
        candidate_urls = [primary_url]

        parsed_primary = urlparse(primary_url)
        # If an internal endpoint is accidentally exposed as HTTPS, try HTTP fallback.
        if (
            parsed_primary.scheme == "https"
            and (parsed_primary.hostname or "").lower() in self._internal_hosts
        ):
            candidate_urls.append(primary_url.replace("https://", "http://", 1))

        last_error = None
        for candidate_url in candidate_urls:
            try:
                open_kwargs = dict(kwargs)
                parsed_candidate = urlparse(candidate_url)
                if parsed_candidate.scheme == "https":
                    client_kwargs = dict(open_kwargs.get("client_kwargs", {}))
                    client_kwargs.setdefault("ssl", False)
                    open_kwargs["client_kwargs"] = client_kwargs
                return self.fs.open(candidate_url, mode=mode, **open_kwargs)
            except Exception as e:
                last_error = e

        raise RuntimeError(
            f"Failed to open streamed file '{clean_path}' from URL '{primary_url}': {last_error}"
        )

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
