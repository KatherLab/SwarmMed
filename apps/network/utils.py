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
    workspaces_root = Path(settings.BASE_DIR) / "workspaces"
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
        # Log this event as it might indicate a provisioning failure
        # or a path mismatch.
        # Ideally we should use the logger here if available, but utils
        # are often standalone. We'll rely on the view to handle the empty buffer.
        return zip_buffer

    # Resolve to absolute path for strict boundary checking
    abs_base_prod_path = base_prod_path.resolve()

    default_admin_startup_dir = abs_base_prod_path / "admin@nvidia.com" / "startup"
    server_startup_dir = abs_base_prod_path / "server" / "startup"
    client_admin_map = {}
    client_server_map = {}
    try:
        if (abs_base_prod_path / ".client_admin_map.json").exists():
            client_admin_map = json.loads(
                (abs_base_prod_path / ".client_admin_map.json").read_text()
            )
            if not isinstance(client_admin_map, dict):
                client_admin_map = {}

        if (abs_base_prod_path / ".client_server_map.json").exists():
            client_server_map = json.loads(
                (abs_base_prod_path / ".client_server_map.json").read_text()
            )
            if not isinstance(client_server_map, dict):
                client_server_map = {}
    except Exception:
        client_admin_map = {}
        client_server_map = {}

    server_dir_names = {"server"}
    server_dir_names.update(
        {
            str(server_name)
            for server_name in client_server_map.values()
            if isinstance(server_name, str) and server_name
        }
    )

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as main_zip:
        # NVFlare creates a directory for each participant
        for item in os.scandir(str(abs_base_prod_path)):
            # We only want to package client kits (not server or admin)
            if (
                item.is_dir()
                and item.name not in server_dir_names
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
                    req_file = (
                        abs_base_prod_path.parent.parent
                        / "docker_compose_requirements.txt"
                    )
                    if req_file.exists():
                        client_zip.write(
                            str(req_file), "docker_compose_requirements.txt"
                        )

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

                    mapped_admin_name = client_admin_map.get(item.name, "")
                    admin_startup_dir = (
                        abs_base_prod_path / mapped_admin_name / "startup"
                        if mapped_admin_name
                        else default_admin_startup_dir
                    )
                    if not admin_startup_dir.exists():
                        admin_startup_dir = default_admin_startup_dir

                    if admin_startup_dir.exists():
                        for root, _, files in os.walk(
                            str(admin_startup_dir), followlinks=False
                        ):
                            for file in files:
                                file_path = Path(root) / file
                                if not file_path.resolve().is_relative_to(
                                    admin_startup_dir
                                ):
                                    continue

                                arcname = Path("admin_startup") / file_path.relative_to(
                                    admin_startup_dir
                                )
                                client_zip.write(str(file_path), str(arcname))

                    selected_startup_dir = None
                    selected_arc_prefix = None

                    mapped_server_name = client_server_map.get(item.name, "")
                    mapped_server_startup_dir = (
                        abs_base_prod_path / mapped_server_name / "startup"
                        if mapped_server_name
                        else server_startup_dir
                    )

                    if mapped_server_startup_dir.exists():
                        selected_startup_dir = mapped_server_startup_dir
                        selected_arc_prefix = "server_startup"
                    elif server_startup_dir.exists():
                        selected_startup_dir = server_startup_dir
                        selected_arc_prefix = "server_startup"

                    if selected_startup_dir and selected_arc_prefix:
                        for root, _, files in os.walk(
                            str(selected_startup_dir), followlinks=False
                        ):
                            for file in files:
                                file_path = Path(root) / file
                                if not file_path.resolve().is_relative_to(
                                    selected_startup_dir
                                ):
                                    continue

                                arcname = Path(
                                    selected_arc_prefix
                                ) / file_path.relative_to(selected_startup_dir)
                                client_zip.write(str(file_path), str(arcname))

                # Add the client's zip file into the main zip buffer
                main_zip.writestr(
                    f"{item.name}.zip", client_zip_buffer.getvalue()
                )

    # If a project-wide requirements file exists in the provision dir, add it to the main zip
    # as a reference for the downloader/uploader.
    req_file = Path(base_prod_path).parent.parent / "docker_compose_requirements.txt"
    if req_file.exists():
        with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED) as main_zip:
            main_zip.write(str(req_file), "docker_compose_requirements.txt")

    zip_buffer.seek(0)
    return zip_buffer
