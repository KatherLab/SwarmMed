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
import socket
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
        self.http_timeout_sec = float(
            os.getenv("SWARMCLOUD_DATA_HTTP_TIMEOUT_SEC", "10").strip() or "10"
        )
        # Keep per-file fallback URL candidates (first item is preferred).
        self._manifest_candidates = {}

        # Load and process the data manifest.
        self.manifest = self._load_manifest()
        
        # Initialize fsspec HTTP filesystem for streaming.
        # We disable SSL verification (ssl=False) to support internal MinIO 
        # instances using self-signed certificates.
        self.fs = fsspec.filesystem(
            "http",
            ssl=False,
            timeout=self.http_timeout_sec,
        )
        
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
        self._manifest_candidates = {}
        
        # 1. Determine the best internal host for reaching the server/coordinator.
        internal_host = os.getenv("SWARMCLOUD_SERVER_HOST", "").strip()
        if internal_host:
            print(f"flare_adapter: Server host from environment: {internal_host}")
        else:
            # Fallback discovery
            for candidate in ["minio", "server", "coordinator"]:
                try:
                    socket.gethostbyname(candidate)
                    internal_host = candidate
                    print(f"flare_adapter: Discovered server host: {internal_host}")
                    break
                except socket.gaierror:
                    continue
            if not internal_host:
                internal_host = "172.17.0.1" # Default Docker Bridge Gateway
                print(f"flare_adapter: Using default bridge gateway: {internal_host}")

        # 2. Process each URL in the manifest.
        for rel_path, url in manifest.items():
            parsed = urlparse(url)
            port = f":{parsed.port}" if parsed.port else ""
            
            # Generate a list of host candidates to try.
            # We prioritize local-to-the-node addresses if USE_LOCAL_DATA is set.
            host_candidates = []
            if self.use_local_data:
                # 'minio' is best if on same docker network.
                # '172.17.0.1' is best if MinIO is bound to host ports.
                host_candidates.extend(["minio", "172.17.0.1", "host.docker.internal", "localhost", "127.0.0.1"])
            
            # Always add the configured internal_host and the original host.
            if internal_host and internal_host not in host_candidates:
                host_candidates.append(internal_host)
            if parsed.hostname and parsed.hostname not in host_candidates:
                host_candidates.append(parsed.hostname)

            # 3. Build the final candidate URL list.
            final_urls = []
            
            # If the user says it should be HTTPS, we prioritize HTTPS candidates.
            for proto in ["https", "http"]:
                for host in host_candidates:
                    new_url = parsed._replace(scheme=proto, netloc=f"{host}{port}").geturl()
                    if new_url not in final_urls:
                        final_urls.append(new_url)
            
            # Ensure the original URL is in the list.
            if url not in final_urls:
                final_urls.append(url)
            
            updated_manifest[rel_path] = final_urls[0]
            self._manifest_candidates[rel_path] = final_urls
            
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
            print(f"flare_adapter: File NOT found in manifest: {path}")
            raise FileNotFoundError(f"File not found in manifest: {path}")

        # Ensure SSL verification is disabled for self-signed certs.
        if "ssl" not in kwargs:
            kwargs["ssl"] = False
        if "timeout" not in kwargs:
            kwargs["timeout"] = self.http_timeout_sec

        candidates = self._manifest_candidates.get(clean_path) or [
            self.manifest[clean_path]
        ]
        deduped_candidates = []
        for url in candidates:
            if url and url not in deduped_candidates:
                deduped_candidates.append(url)

        if not deduped_candidates:
            print(f"flare_adapter: No valid URL candidates for: {path}")
            raise FileNotFoundError(f"No valid URL candidates for: {path}")

        print(f"flare_adapter: Opening {clean_path} with {len(deduped_candidates)} candidates.")

        return _FallbackHTTPStream(
            fs=self.fs,
            path_label=clean_path,
            candidates=deduped_candidates,
            mode=mode,
            kwargs=kwargs,
        )

    def read_bytes(self, path: str) -> bytes:
        """Reads all bytes from a file."""
        print(f"flare_adapter: Reading bytes from {path}")
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


class _FallbackHTTPStream:
    """Read-through stream with automatic fallback to alternate URLs."""

    def __init__(self, fs, path_label: str, candidates: List[str], mode: str, kwargs: dict):
        self._fs = fs
        self._path_label = path_label
        self._candidates = candidates
        self._mode = mode
        self._kwargs = dict(kwargs)
        self._index = -1
        self._stream = None
        self._open_next(current_pos=0, announce=False)

    def _open_next(self, current_pos: int, announce: bool):
        last_error = None
        while self._index + 1 < len(self._candidates):
            self._index += 1
            candidate = self._candidates[self._index]
            try:
                stream = self._fs.open(candidate, mode=self._mode, **self._kwargs)
                if current_pos:
                    try:
                        stream.seek(current_pos)
                    except Exception:
                        # Best effort: advance by reading when seek is unavailable.
                        _ = stream.read(current_pos)
                self._stream = stream
                if announce and self._index > 0:
                    print(
                        "FlareDataFileSystem: primary URL failed; "
                        f"using fallback URL for {self._path_label}"
                    )
                return
            except Exception as e:
                last_error = e

        if last_error:
            raise last_error
        raise FileNotFoundError(
            f"No remaining URL candidates for: {self._path_label}"
        )

    def _with_fallback(self, method_name: str, *args, **kwargs):
        while True:
            try:
                method = getattr(self._stream, method_name)
                return method(*args, **kwargs)
            except Exception as e:
                current_pos = 0
                try:
                    current_pos = int(self._stream.tell())
                except Exception:
                    current_pos = 0

                if self._index + 1 >= len(self._candidates):
                    raise e

                self._open_next(current_pos=current_pos, announce=True)

    def read(self, *args, **kwargs):
        return self._with_fallback("read", *args, **kwargs)

    def readline(self, *args, **kwargs):
        return self._with_fallback("readline", *args, **kwargs)

    def readinto(self, *args, **kwargs):
        return self._with_fallback("readinto", *args, **kwargs)

    def seek(self, *args, **kwargs):
        return self._with_fallback("seek", *args, **kwargs)

    def tell(self):
        return self._with_fallback("tell")

    def close(self):
        if self._stream is not None:
            self._stream.close()

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if line in (b"", ""):
            raise StopIteration
        return line

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __getattr__(self, item):
        return getattr(self._stream, item)


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
