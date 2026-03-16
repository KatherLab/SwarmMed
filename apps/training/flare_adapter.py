"""
NVIDIA FLARE Adapter for MedSwarmHub.
This module provides a simplified, streaming interface for training scripts to
interact with the NVFlare system and the project's S3 data storage.
"""

import json
import os
import shutil
import ssl
import tempfile
import socket
import time
from typing import Any, List, Optional
from urllib.parse import urlparse

import numpy as np
import nvflare.client as flare
import fsspec
import requests
from dotenv import load_dotenv

# Load environment variables from a local .env file if it exists.
load_dotenv()


class FlareDataFileSystem:
    """
    A streaming virtual filesystem for NVFlare jobs powered by fsspec.
    Provides on-demand access to data stored in MinIO via signed URLs
    fetched from the secure local app proxy.
    """

    def __init__(self, project_uuid: str):
        self.project_uuid = (
            os.getenv("MEDSWARMHUB_PROJECT_ID", "").strip() or project_uuid
        )
        self.use_local_data = (
            os.getenv("MEDSWARMHUB_USE_LOCAL_DATA", "1").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.http_timeout_sec = float(
            os.getenv("MEDSWARMHUB_DATA_HTTP_TIMEOUT_SEC", "30").strip() or "30"
        )
        # Keep per-file fallback URL candidates (first item is preferred).
        self._manifest_candidates = {}
        # Cache for the first successful host candidate to speed up subsequent opens
        self._working_host_prefix = None

        # Load and process the data manifest.
        self.manifest = self._load_manifest()
        
        # Initialize fsspec HTTP filesystem for streaming.
        # Request-level SSL verification is handled in self.open().
        self.fs = fsspec.filesystem(
            "http",
            timeout=self.http_timeout_sec,
        )
        
        # Create a temporary directory only for legacy compatibility or if explicitly needed.
        self.temp_dir = tempfile.mkdtemp(prefix=f"flare_{self.project_uuid}_")

    def _load_manifest(self) -> dict:
        # 1. Fetch manifest from the secure App Proxy API.
        # We MUST prioritize the node's local app proxy (localhost/gateway).
        manifest_secret = os.getenv("MANIFEST_SECRET")
        if manifest_secret and self.project_uuid:
            # Try multiple hosts to reach the local Django app. 
            # We check port 5085 (Nginx proxy) and 8000 (direct app container).
            discovery_targets = [
                ("100.127.11.1", 5085), # Host Machine IP
                ("172.17.0.1", 5085),
                ("localhost", 5085),
                ("127.0.0.1", 5085),
                ("medswarmhub", 8000),
                ("host.docker.internal", 5085)
            ]
            
            # Also add configured host as fallback
            env_host = os.getenv("MEDSWARMHUB_SERVER_HOST", "").strip()
            if env_host:
                discovery_targets.append((env_host, 5085))

            for host, port in discovery_targets:
                print(f"flare_adapter: Fetching secure manifest from app proxy at {host}:{port}...")
                try:
                    url = f"https://{host}:{port}/data/manifest/?project_id={self.project_uuid}"
                    try:
                        resp = requests.get(
                            url,
                            headers={"X-Manifest-Secret": manifest_secret},
                            timeout=3,
                            verify=os.getenv("MEDSWARMHUB_CA_CERT", "/usr/local/share/ca-certificates/internal-ca.crt"),
                        )
                        if resp.status_code == 200:
                            manifest = resp.json()
                            if manifest:
                                print(f"flare_adapter: Securely loaded manifest with {len(manifest)} files from {host}:{port}.")
                                return self._process_manifest_urls(manifest)
                            else:
                                print(f"flare_adapter: App proxy at {host}:{port} returned an empty manifest.")
                    except Exception:
                        continue
                except Exception as e:
                    print(f"flare_adapter: Error connecting to app proxy at {host}:{port}: {e}")

        # 2. Fallback: Attempt legacy file-based manifest if present (e.g. for debugging)
        for loc in [os.path.join(os.getcwd(), "data_manifest.json"), os.path.join(os.getcwd(), "custom", "data_manifest.json")]:
            if os.path.exists(loc):
                try:
                    with open(loc) as f:
                        print(f"flare_adapter: Loaded legacy manifest from {loc}")
                        return self._process_manifest_urls(json.load(f))
                except Exception: continue
        
        print("flare_adapter: ERROR: Could not load data manifest from any source.")
        return {}

    def _process_manifest_urls(self, manifest: dict) -> dict:
        """Adds host/protocol fallbacks to manifest URLs for maximum resilience."""
        processed = {}
        self._manifest_candidates = {}
        
        # Determine local networking candidates
        # We include the 100.127.11.1 IP as it's the confirmed reachable host.
        local_ips = ["100.127.11.1", "minio", "host.docker.internal", "localhost", "127.0.0.1", "172.17.0.1"]
        try:
            container_ip = socket.gethostbyname(socket.gethostname())
            if container_ip not in local_ips: local_ips.append(container_ip)
        except Exception: pass

        for rel_path, url in manifest.items():
            parsed = urlparse(url)
            port = f":{parsed.port}" if parsed.port else ""
            original_proto = parsed.scheme or "https"
            
            final_urls = []
            
            # 1. Prioritize the ORIGINAL URL from manifest
            final_urls.append(url)

            # 2. Add local fallback candidates
            protocols = [original_proto]
            if original_proto == "https": protocols.append("http")
            else: protocols.append("https")

            for host in local_ips:
                for proto in protocols:
                    new_url = parsed._replace(scheme=proto, netloc=f"{host}{port}").geturl()
                    if new_url not in final_urls: final_urls.append(new_url)
            
            processed[rel_path] = final_urls[0]
            self._manifest_candidates[rel_path] = final_urls
            
        return processed

    def ls(self, path: str = "") -> List[str]:
        """Lists available files in the virtual filesystem."""
        path = path.strip("/")
        if not path:
            return list(self.manifest.keys())
        results = set()
        for p in self.manifest.keys():
            if p.startswith(path):
                rel = p[len(path):].lstrip("/")
                part = rel.split("/")[0]
                if part: results.add(part)
        return sorted(list(results))

    def glob(self, pattern: str) -> List[str]:
        """Simple glob matching for manifest paths."""
        import fnmatch
        return [p for p in self.manifest.keys() if fnmatch.fnmatch(p, pattern)]

    def exists(self, path: str) -> bool:
        """Checks if a path exists in the manifest."""
        path = path.lstrip("/")
        if path in self.manifest: return True
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
        Attempts all candidate URLs until one succeeds.
        """
        clean_path = path.lstrip("/")
        if clean_path not in self.manifest:
            raise FileNotFoundError(f"File not found in manifest: {path}")

        # If we already found a working host prefix, prioritize it
        candidates = self._manifest_candidates.get(clean_path) or [self.manifest[clean_path]]
        ordered_candidates = []
        
        if self._working_host_prefix:
            for url in candidates:
                if url.startswith(self._working_host_prefix):
                    ordered_candidates.append(url)
                    break
        
        for url in candidates:
            if url and url not in ordered_candidates:
                ordered_candidates.append(url)

        last_error = None
        # Use a short timeout for probing to avoid hanging
        probe_timeout = 3.0 if not self._working_host_prefix else self.http_timeout_sec
        
        for url in ordered_candidates:
            try:
                # We pass ssl=False to individual requests to support internal MinIO.
                kwargs.setdefault("ssl", False)
                
                # Check metadata first if we don't have a working prefix
                if not self._working_host_prefix:
                    print(f"flare_adapter: Probing candidate: {url} (timeout={probe_timeout}s)")
                    try:
                        # info() is lighter than open() for checking reachability
                        self.fs.info(url, timeout=probe_timeout, ssl=False)
                        # Success! Cache the prefix (protocol + host + port)
                        parsed = urlparse(url)
                        self._working_host_prefix = f"{parsed.scheme}://{parsed.netloc}"
                        print(f"flare_adapter: FOUND working data host: {self._working_host_prefix}")
                    except Exception as e:
                        print(f"flare_adapter: Candidate {url} unreachable: {e}")
                        continue

                # Final open with full timeout
                print(f"flare_adapter: Streaming from: {url}")
                return self.fs.open(url, mode=mode, timeout=self.http_timeout_sec, **kwargs)
                
            except Exception as e:
                print(f"flare_adapter: Error opening {url}: {e}")
                last_error = e

        if last_error: raise last_error
        raise FileNotFoundError(f"All URL candidates failed for: {clean_path}")

    def read_bytes(self, path: str) -> bytes:
        """Reads all bytes from a file."""
        with self.open(path, "rb") as f: return f.read()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Reads all text from a file."""
        with self.open(path, "r", encoding=encoding) as f: return f.read()

    def __getitem__(self, path: str):
        """Allows fs['path'] access."""
        return self.read_bytes(path)

    def get_data_path(self) -> str:
        """Legacy Interface: Materializes all data to a local temp directory."""
        print("flare_adapter: Materializing data to local temp directory (Legacy Mode)...")
        for rel_path in self.manifest.keys():
            local_path = os.path.join(self.temp_dir, rel_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with self.open(rel_path, "rb") as remote_f, open(local_path, "wb") as local_f:
                shutil.copyfileobj(remote_f, local_f)
        return self.temp_dir

    def __enter__(self): return self

    def __exit__(self, exc_type, exc_val, exc_tb): self.cleanup()

    def cleanup(self):
        if os.path.exists(self.temp_dir): shutil.rmtree(self.temp_dir)


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
            if "numpy_key" in input_model.params and len(input_model.params) == 1:
                input_model.params = {}
        return input_model
    except Exception as e:
        print(f"flare_adapter: Error receiving model: {e}")
        return None


def send_model(params, metrics: dict = None, meta: dict = None):
    """Sends model updates and metrics back to the server."""
    if params is None: raise ValueError("flare_adapter: send_model received params=None.")
    if isinstance(params, (list, tuple)):
        params = {str(i): v for i, v in enumerate(params)}
    params = _ensure_transportable(params)
    meta = meta or {}
    if "NUM_STEPS_CURRENT_ROUND" in meta:
        meta["aggregation_weight"] = meta["NUM_STEPS_CURRENT_ROUND"]
    elif "aggregation_weight" not in meta:
        meta["aggregation_weight"] = 1.0
    output_model = flare.FLModel(params=params, metrics=metrics or {}, meta=meta)
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
