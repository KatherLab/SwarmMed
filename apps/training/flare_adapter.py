"""NVIDIA FLARE Adapter for MedSwarmHub.

This module provides a simplified, streaming interface for training scripts to
interact with the NVFlare system and the project's S3 data storage.
"""

import json
import os
import shutil
import socket
import tempfile
from urllib.parse import urlparse

import fsspec
import numpy as np
import nvflare.client as flare
import nvflare.client.lightning # Ensure lightning is accessible through flare.lightning
import requests
from dotenv import load_dotenv

# Load environment variables from a local .env file if it exists.
load_dotenv()


class FlareDataFileSystem:
    """A streaming virtual filesystem for NVFlare jobs powered by fsspec.

    Provides on-demand access to data stored in MinIO via signed URLs
    fetched from the secure local app proxy.

    Attributes:
        project_uuid (str): The unique identifier for the project.
        use_local_data (bool): Whether to use local data.
        http_timeout_sec (float): HTTP timeout in seconds.
        manifest (dict): A mapping of relative paths to URLs.
        fs (fsspec.filesystem): The fsspec HTTP filesystem instance.
        temp_dir (str): Path to a temporary directory.
    """

    def __init__(self, project_uuid: str):
        """Initializes the FlareDataFileSystem.

        Args:
            project_uuid (str): The unique identifier for the project.
        """
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
        """Fetches the data manifest from the secure App Proxy API or legacy file.

        Returns:
            dict: A dictionary mapping relative paths to URLs.
        """
        # 1. Fetch manifest from the secure App Proxy API.
        # We MUST prioritize the node's local app proxy (localhost/gateway).
        manifest_secret = os.getenv("MANIFEST_SECRET")
        docker_host_ip = os.getenv("DOCKER_HOST_IP", "172.17.0.1")
        if manifest_secret and self.project_uuid:
            # Try multiple hosts to reach the local Django app.
            # We check port 5085 (Nginx proxy) and 8000 (direct app container).
            discovery_targets = [
                (docker_host_ip, 5085),
                ("localhost", 5085),
                ("127.0.0.1", 5085),
                ("medswarmhub", 8000),
                ("host.docker.internal", 5085),
            ]

            # Also add configured host as fallback
            env_host = os.getenv("MEDSWARMHUB_SERVER_HOST", "").strip()
            if env_host:
                discovery_targets.append((env_host, 5085))

            for host, port in discovery_targets:
                print(
                    f"flare_adapter: Fetching secure manifest from app proxy at {host}:{port}..."
                )
                try:
                    url = f"https://{host}:{port}/data/manifest/?project_id={self.project_uuid}"
                    try:
                        resp = requests.get(
                            url,
                            headers={"X-Manifest-Secret": manifest_secret},
                            timeout=3,
                            verify=os.getenv(
                                "MEDSWARMHUB_CA_CERT",
                                "/usr/local/share/ca-certificates/internal-ca.crt",
                            ),
                        )
                        if resp.status_code == 200:
                            manifest = resp.json()
                            if manifest:
                                print(
                                    f"flare_adapter: Securely loaded manifest with {len(manifest)} files from {host}:{port}."
                                )
                                return self._process_manifest_urls(manifest)
                            else:
                                print(
                                    f"flare_adapter: App proxy at {host}:{port} returned an empty manifest."
                                )
                    except Exception:
                        continue
                except Exception as e:
                    print(
                        f"flare_adapter: Error connecting to app proxy at {host}:{port}: {e}"
                    )

        # 2. Fallback: Attempt legacy file-based manifest if present (e.g. for debugging)
        for loc in [
            os.path.join(os.getcwd(), "data_manifest.json"),
            os.path.join(os.getcwd(), "custom", "data_manifest.json"),
        ]:
            if os.path.exists(loc):
                try:
                    with open(loc) as f:
                        print(f"flare_adapter: Loaded legacy manifest from {loc}")
                        return self._process_manifest_urls(json.load(f))
                except Exception:
                    continue

        print(
            "flare_adapter: ERROR: Could not load data manifest from any source."
        )
        return {}

    def _process_manifest_urls(self, manifest: dict) -> dict:
        """Adds host/protocol fallbacks to manifest URLs for maximum resilience.

        Args:
            manifest (dict): The raw manifest dictionary.

        Returns:
            dict: The processed manifest dictionary.
        """
        processed = {}
        self._manifest_candidates = {}

        # Determine local networking candidates
        # We include the 100.127.11.1 IP as it's the confirmed reachable host.
        gateway_ip = os.getenv("DOCKER_HOST_IP", "172.17.0.1")
        local_ips = [
            "100.127.11.1",
            "minio",
            "host.docker.internal",
            "localhost",
            "127.0.0.1",
            gateway_ip,
        ]

        # If we are on the host network, 'minio' won't resolve.
        # We check if we can resolve 'minio', and if not, we use the gateway.
        try:
            socket.gethostbyname("minio")
        except socket.gaierror:
            # If 'minio' is in the candidates but doesn't resolve,
            # ensure the gateway is tried early.
            if gateway_ip not in local_ips:
                local_ips.insert(0, gateway_ip)
            # We can also dynamically add it to /etc/hosts if we have permission,
            # but usually just trying the IP is safer and more reliable.

        try:
            container_ip = socket.gethostbyname(socket.gethostname())
            if container_ip not in local_ips:
                local_ips.append(container_ip)
        except Exception:
            pass

        for rel_path, url in manifest.items():
            parsed = urlparse(url)
            # Use netloc to preserve port if present, but we might need to swap the host part
            original_port = parsed.port
            original_proto = parsed.scheme or "https"

            final_urls = []

            # 1. Prioritize the ORIGINAL URL from manifest
            final_urls.append(url)

            # 2. Add local fallback candidates
            protocols = [original_proto]
            if original_proto == "https":
                protocols.append("http")
            else:
                protocols.append("https")

            for host in local_ips:
                for proto in protocols:
                    # Construct netloc correctly with port
                    netloc = host
                    if original_port:
                        netloc = f"{host}:{original_port}"

                    new_url = parsed._replace(
                        scheme=proto, netloc=netloc
                    ).geturl()
                    if new_url not in final_urls:
                        final_urls.append(new_url)

            processed[rel_path] = final_urls[0]
            self._manifest_candidates[rel_path] = final_urls

        return processed

    def ls(self, path: str = "") -> list[str]:
        """Lists available files in the virtual filesystem.

        Args:
            path (str): The directory path to list. Defaults to "".

        Returns:
            List[str]: A list of filenames or directory names.
        """
        path = path.strip("/")
        if not path:
            return list(self.manifest.keys())
        results = set()
        for p in self.manifest:
            if p.startswith(path):
                rel = p[len(path) :].lstrip("/")
                part = rel.split("/")[0]
                if part:
                    results.add(part)
        return sorted(list(results))

    def glob(self, pattern: str) -> list[str]:
        """Simple glob matching for manifest paths.

        Args:
            pattern (str): The glob pattern to match.

        Returns:
            List[str]: A list of matching paths.
        """
        import fnmatch

        return [
            p for p in self.manifest if fnmatch.fnmatch(p, pattern)
        ]

    def exists(self, path: str) -> bool:
        """Checks if a path exists in the manifest.

        Args:
            path (str): The path to check.

        Returns:
            bool: True if the path exists, False otherwise.
        """
        path = path.lstrip("/")
        if path in self.manifest:
            return True
        prefix = path.rstrip("/") + "/"
        return any(k.startswith(prefix) for k in self.manifest)

    def isfile(self, path: str) -> bool:
        """Checks if path is a file.

        Args:
            path (str): The path to check.

        Returns:
            bool: True if it's a file, False otherwise.
        """
        return path.lstrip("/") in self.manifest

    def isdir(self, path: str) -> bool:
        """Checks if path is a directory prefix.

        Args:
            path (str): The path to check.

        Returns:
            bool: True if it's a directory, False otherwise.
        """
        prefix = path.lstrip("/").rstrip("/") + "/"
        return any(k.startswith(prefix) for k in self.manifest)

    def open(self, path: str, mode: str = "rb", **kwargs):
        """Streams a file from the manifest.

        Returns a file-like object that can be passed to pandas, torch, etc.
        Attempts all candidate URLs until one succeeds.

        Args:
            path (str): The path to the file.
            mode (str): The file opening mode. Defaults to "rb".
            **kwargs: Additional arguments for fsspec.

        Returns:
            IO: A file-like object.

        Raises:
            FileNotFoundError: If the file is not found or all candidates fail.
        """
        clean_path = path.lstrip("/")
        if clean_path not in self.manifest:
            raise FileNotFoundError(f"File not in manifest: {path}")

        # If we already found a working host prefix, prioritize it
        candidates = self._manifest_candidates.get(clean_path) or [
            self.manifest[clean_path]
        ]
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
        probe_timeout = (
            3.0 if not self._working_host_prefix else self.http_timeout_sec
        )

        for url in ordered_candidates:
            try:
                # We pass ssl=False to individual requests to support internal MinIO.
                kwargs.setdefault("ssl", False)

                # Check metadata first if we don't have a working prefix
                if not self._working_host_prefix:
                    print(
                        f"flare_adapter: Probing candidate: {url} (timeout={probe_timeout}s)"
                    )
                    try:
                        # We use a streaming GET request to probe instead of info()
                        # because signed URLs for GET requests will fail a HEAD request (used by info).
                        # We only check the status code to confirm reachability.
                        with requests.get(
                            url, stream=True, timeout=probe_timeout, verify=False
                        ) as r:
                            if r.status_code < 400:
                                # Success! Cache the prefix (protocol + host + port)
                                parsed = urlparse(url)
                                self._working_host_prefix = (
                                    f"{parsed.scheme}://{parsed.netloc}"
                                )
                                print(
                                    f"flare_adapter: FOUND working data host: {self._working_host_prefix}"
                                )
                            else:
                                print(
                                    f"flare_adapter: Candidate {url} returned status {r.status_code}"
                                )
                                continue
                    except Exception as e:
                        print(
                            f"flare_adapter: Candidate {url} unreachable: {e}"
                        )
                        continue

                # Final open with full timeout
                print(f"flare_adapter: Streaming from: {url}")
                return self.fs.open(
                    url, mode=mode, timeout=self.http_timeout_sec, **kwargs
                )

            except Exception as e:
                print(f"flare_adapter: Error opening {url}: {e}")
                last_error = e

        if last_error:
            raise last_error
        raise FileNotFoundError(f"All URL candidates failed for: {clean_path}")

    def read_bytes(self, path: str) -> bytes:
        """Reads all bytes from a file.

        Args:
            path (str): The path to the file.

        Returns:
            bytes: The file content in bytes.
        """
        with self.open(path, "rb") as f:
            return f.read()

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Reads all text from a file.

        Args:
            path (str): The path to the file.
            encoding (str): The text encoding. Defaults to "utf-8".

        Returns:
            str: The file content as text.
        """
        with self.open(path, "r", encoding=encoding) as f:
            return f.read()

    def __getitem__(self, path: str):
        """Allows fs['path'] access.

        Args:
            path (str): The path to the file.

        Returns:
            bytes: The file content in bytes.
        """
        return self.read_bytes(path)

    def get_data_path(self) -> str:
        """Legacy Interface: Materializes all data to a local temp directory.

        Returns:
            str: The local path to the materialized data.
        """
        print(
            "flare_adapter: Materializing data to local temp directory (Legacy Mode)..."
        )
        for rel_path in self.manifest:
            local_path = os.path.join(self.temp_dir, rel_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with self.open(
                rel_path, "rb"
            ) as remote_f, open(local_path, "wb") as local_f:
                shutil.copyfileobj(remote_f, local_f)
        return self.temp_dir

    def __enter__(self):
        """Context manager entry point.

        Returns:
            FlareDataFileSystem: The FlareDataFileSystem instance.
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit point.

        Performs cleanup of temporary resources.

        Args:
            exc_type: The type of the exception.
            exc_val: The exception instance.
            exc_tb: The traceback object.
        """
        self.cleanup()

    def cleanup(self):
        """Cleans up temporary directory and other resources."""
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)


# =================================================================================
# Public Adapter Functions
# =================================================================================


def init_flare():
    """Initializes the NVIDIA FLARE client."""
    flare.init()
    print("flare_adapter: NVIDIA FLARE client initialized.")


def get_data_filesystem(project_id: str) -> FlareDataFileSystem:
    """Factory function to create a FlareDataFileSystem instance.

    Args:
        project_id (str): The project unique identifier.

    Returns:
        FlareDataFileSystem: An instance of FlareDataFileSystem.
    """
    return FlareDataFileSystem(project_uuid=project_id)


def receive_model():
    """Receives the latest global model from the server.

    Returns:
        FLModel: The received global model, or None if an error occurred.
    """
    print("flare_adapter: Receiving global model...")
    try:
        input_model = flare.receive()
        if input_model and input_model.params and (
            "numpy_key" in input_model.params
            and len(input_model.params) == 1
        ):
            input_model.params = {}
        return input_model
    except Exception as e:
        print(f"flare_adapter: Error receiving model: {e}")
        return None


def send_model(params, metrics: dict = None, meta: dict = None):
    """Sends model updates and metrics back to the server.

    Args:
        params: The model parameters to send. Could be a dictionary, list/tuple, or FLModel.
        metrics (dict, optional): Optional dictionary of metrics.
        meta (dict, optional): Optional dictionary of metadata.

    Raises:
        ValueError: If params is None.
    """
    if params is None:
        raise ValueError("flare_adapter: send_model received params=None.")

    # If an FLModel is passed, extract its components
    if isinstance(params, flare.FLModel):
        metrics = metrics or params.metrics
        meta = meta or params.meta
        params = params.params

    if isinstance(params, (list, tuple)):
        params = {str(i): v for i, v in enumerate(params)}
    params = _ensure_transportable(params)
    meta = meta or {}
    if "NUM_STEPS_CURRENT_ROUND" in meta:
        meta["aggregation_weight"] = meta["NUM_STEPS_CURRENT_ROUND"]
    elif "aggregation_weight" not in meta:
        meta["aggregation_weight"] = 1.0
    output_model = flare.FLModel(
        params=params, metrics=metrics or {}, meta=meta
    )
    flare.send(output_model)
    print("flare_adapter: Model sent to server.")


def get_weights_list(params: dict):
    """Helper for Keras model.set_weights().

    Args:
        params (dict): Dictionary of model parameters.

    Returns:
        list: A list of numpy arrays.
    """
    try:
        keys = sorted(params.keys(), key=lambda x: int(x))
        return [np.array(params[k]) for k in keys]
    except Exception:
        return [np.array(v) for k, v in sorted(params.items())]


def get_pytorch_state_dict(params: dict):
    """Helper for PyTorch model.load_state_dict().

    Args:
        params (dict): Dictionary of model parameters.

    Returns:
        dict: A dictionary with torch tensors.
    """
    import torch

    return {k: torch.as_tensor(v) for k, v in params.items()}


def _ensure_transportable(params: dict):
    """Converts tensors and other formats to numpy for transport.

    Args:
        params (dict): Dictionary of model parameters.

    Returns:
        dict: A dictionary with numpy arrays.
    """
    converted = {}
    for k, v in params.items():
        if hasattr(v, "detach") and hasattr(v, "cpu"):
            converted[k] = v.detach().cpu().numpy()
        elif hasattr(v, "numpy"):
            converted[k] = v.numpy()
        else:
            converted[k] = np.array(v)
    return converted


# =================================================================================
# Public Client API Aliases
# =================================================================================
# These aliases allow 'import flare_adapter as flare' for a unified experience,
# providing the best of both the original NVFlare client and our adapter.

init = init_flare
receive = receive_model
send = send_model
is_running = flare.is_running
FLModel = flare.FLModel
lightning = nvflare.client.lightning
flare = flare
