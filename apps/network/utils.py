"""
Utility functions for the network app.
Provides helpers for Tailscale integration, machine identification,
and packaging startup kits into zip files.
"""

import io
import json
import os
import random
import shutil
import socket

# Bandit B404: subprocess is required for tailscale CLI integration; no shell=True usage.
import subprocess  # nosec B404
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.utils.text import slugify

TAILSCALE_STATUS_BASE_URL = os.environ.get("TAILSCALE_STATUS_BASE_URL")


def _fetch_tailscale_status():
    """Fetch Tailscale status from the dedicated sidecar if configured."""
    if not TAILSCALE_STATUS_BASE_URL:
        return None

    status_url = f"{TAILSCALE_STATUS_BASE_URL.rstrip('/')}/status"
    if not status_url.startswith(("http://", "https://")):
        return None

    try:
        # Bandit B310: url is validated as http(s) and points at a configured sidecar; short timeout.
        with urllib.request.urlopen(
            status_url, timeout=2
        ) as response:  # nosec B310
            data = response.read().decode("utf-8")
            return json.loads(data)
    except (urllib.error.URLError, ValueError, TimeoutError):
        return None


def get_tailscale_ip():
    """
    Retrieves the current machine's Tailscale IPv4 address using the
    tailscale CLI. The result is cached for 5 minutes to improve performance.
    """
    cached_ip = cache.get("tailscale_ip")
    if cached_ip:
        return cached_ip

    payload = _fetch_tailscale_status()
    if payload and payload.get("ipv4"):
        cache.set("tailscale_ip", payload["ipv4"], 300)
        return payload["ipv4"]

    try:
        # Run 'tailscale ip --4' to get the local Tailscale IPv4 address
        tailscale_path = shutil.which("tailscale") or "tailscale"
        # Bandit B603: args are a fixed list; shell=False; binary resolved via shutil.which.
        result = subprocess.run(  # nosec B603
            [tailscale_path, "ip", "--4"],
            capture_output=True,
            text=True,
            check=True,
        )
        ip = result.stdout.strip()

        # Store in cache (300 seconds = 5 minutes)
        cache.set("tailscale_ip", ip, 300)
        return ip

    except PermissionError:
        return "Permission Denied"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "Not Available"


def is_tailscale_connected():
    """
    Checks if Tailscale is currently connected by inspecting the status output.
    Returns a string: "connected", "disconnected", or "Permission Denied".
    """
    cached_state = cache.get("tailscale_connected")
    if cached_state is not None:
        return cached_state

    payload = _fetch_tailscale_status()
    if payload and "connected" in payload:
        status = "connected" if payload["connected"] else "disconnected"
        cache.set("tailscale_connected", status, 30)
        return status

    try:
        # 'tailscale status' returns information about the node and its peers
        tailscale_path = shutil.which("tailscale") or "tailscale"
        # Bandit B603: args are a fixed list; shell=False; binary resolved via shutil.which.
        result = subprocess.run(  # nosec B603
            [tailscale_path, "status"],
            capture_output=True,
            text=True,
            check=True,
        )

        # Logic: If status is not empty and we are not in a restricted
        # 'peerapi' state
        is_connected = (
            bool(result.stdout.strip())
            and "peerapi" not in result.stdout.lower()
        )

        status = "connected" if is_connected else "disconnected"
        # Short cache for status as it can change frequently
        cache.set("tailscale_connected", status, 30)
        return status

    except PermissionError:
        cache.set("tailscale_connected", "Permission Denied", 10)
        return "Permission Denied"
    except (subprocess.CalledProcessError, FileNotFoundError):
        cache.set("tailscale_connected", "disconnected", 10)
        return "disconnected"


def get_hostname():
    """
    Returns a unique, human-friendly hostname for the current machine.
    
    1. Checks environment variable SWARMCLOUD_HOSTNAME.
    2. Checks for a persisted hostname in a local file.
    3. Generates and persists a new random human-friendly name if none exists.
    """
    # 1. Environment variable override
    env_hostname = os.environ.get("SWARMCLOUD_HOSTNAME")
    if env_hostname:
        return env_hostname

    # Path to the persisted hostname file
    hostname_file = Path(settings.BASE_DIR) / ".swarmcloud_hostname"

    # 2. Check for persisted hostname
    if hostname_file.exists():
        try:
            with open(hostname_file, "r") as f:
                persisted_name = f.read().strip()
                if persisted_name:
                    return persisted_name
        except Exception:
            pass

    # 3. Generate a new human-friendly hostname
    adjectives = [
        "agile", "brave", "bright", "calm", "clever", "cool", "eager", "fancy", 
        "gentle", "happy", "jolly", "kind", "lively", "mighty", "nice", "proud", 
        "quick", "rare", "sharp", "smart", "swift", "tall", "vivid", "wild", 
        "wise", "young", "bold", "keen", "grand", "super"
    ]
    nouns = [
        "ant", "bear", "bird", "cat", "deer", "dog", "eagle", "fish", "fox", "frog",
        "goat", "hawk", "lion", "lynx", "mole", "mouse", "owl", "panda", "puma", "rabbit",
        "seal", "shark", "tiger", "wolf", "zebra", "crane", "swan", "falcon", "badger", "otter"
    ]
    
    new_hostname = f"{random.choice(adjectives)}-{random.choice(nouns)}"
    
    # Persist it for future use
    try:
        with open(hostname_file, "w") as f:
            f.write(new_hostname)
    except Exception:
        # If persistence fails, we still return the generated name for this session
        pass

    return new_hostname


def create_startup_kits_zip(swarm_network):
    """
    Gathers all client startup kits generated by NVFlare and packages
    them into a single ZIP file for the user to download.

    Args:
        swarm_network: The SwarmNetwork instance.

    Returns:
        io.BytesIO: A buffer containing the zip file data.
    """
    # Sanitize project name to prevent path traversal
    project_name = slugify(swarm_network.project.title).replace("-", "_")

    # The path where NVFlare 'provision' command saves its outputs
    # We use Path for more robust path handling
    workspaces_root = Path(os.getcwd()) / "workspaces"
    base_prod_path = (
        workspaces_root
        / str(swarm_network.project.identifier)
        / str(swarm_network.identifier)
        / "workspace"
        / project_name
        / "prod_00"
    )

    zip_buffer = io.BytesIO()

    if not base_prod_path.exists():
        return zip_buffer

    # Resolve to absolute path for strict boundary checking
    abs_base_prod_path = base_prod_path.resolve()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as main_zip:
        # NVFlare creates a directory for each participant
        for item in os.scandir(str(abs_base_prod_path)):
            # We only want to package client kits (not server or admin)
            if (
                item.is_dir()
                and item.name != "server"
                and "admin" not in item.name
            ):
                # Ensure client_dir_path is strictly within base_prod_path
                client_dir = Path(item.path).resolve()
                if not client_dir.is_relative_to(abs_base_prod_path):
                    continue

                # Create an internal zip for this specific client
                client_zip_buffer = io.BytesIO()
                with zipfile.ZipFile(
                    client_zip_buffer, "w", zipfile.ZIP_DEFLATED
                ) as client_zip:
                    # os.walk is safe here as client_dir is validated
                    for root, _, files in os.walk(
                        str(client_dir), followlinks=False
                    ):
                        for file in files:
                            file_path = Path(root) / file
                            # Ensure individual files are also within the client directory
                            if not file_path.resolve().is_relative_to(
                                client_dir
                            ):
                                continue

                            # Relative path inside the client-specific zip
                            arcname = file_path.relative_to(client_dir)
                            client_zip.write(str(file_path), str(arcname))

                # Add the client's zip file into the main zip buffer
                main_zip.writestr(
                    f"{item.name}.zip", client_zip_buffer.getvalue()
                )

    zip_buffer.seek(0)
    return zip_buffer
