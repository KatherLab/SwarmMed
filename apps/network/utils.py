"""Utility functions for the network app.
Provides helpers for Tailscale integration, machine identification,
and packaging startup kits into zip files.
"""

import io
import json
import os
import random
import shutil

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
    """Fetches Tailscale status from the dedicated sidecar if configured.

    Returns:
        dict: The Tailscale status JSON, or None if failed.
    """
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
    """Retrieves the current machine's Tailscale IPv4 address.

    The result is cached for 5 minutes to improve performance.

    Returns:
        str: The Tailscale IP address, or 'Not Available' if failed.
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
    """Checks if Tailscale is currently connected.

    Returns:
        str: "connected", "disconnected", or "Permission Denied".
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
    """Returns a unique, human-friendly hostname for the current machine.
    
    1. Checks environment variable MEDSWARMHUB_HOSTNAME.
    2. Checks for a persisted hostname in a local file.
    3. Generates and persists a new random human-friendly name if none exists.

    Returns:
        str: The human-friendly hostname.
    """
    # 1. Environment variable override
    env_hostname = os.environ.get("MEDSWARMHUB_HOSTNAME")
    if env_hostname:
        return env_hostname

    # Path to the persisted hostname file
    hostname_file = Path(settings.BASE_DIR) / ".medswarmhub_hostname"

    # 2. Check for persisted hostname
    if hostname_file.exists():
        try:
            with open(hostname_file) as f:
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
    """Gathers all client startup kits and packages them into a single ZIP file.

    Args:
        swarm_network (SwarmNetwork): The swarm network instance.

    Returns:
        io.BytesIO: A buffer containing the ZIP file data.
    """
    import secrets

    project_name = slugify(swarm_network.project.title).replace("-", "_")
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
        return zip_buffer

    abs_base_prod_path = base_prod_path.resolve()
    
    # Participant Metadata
    participants_data = []
    participant_tokens = {}
    for p in swarm_network.participants.all():
        if not p.gossip_token:
            p.gossip_token = secrets.token_hex(32)
            p.save(update_fields=["gossip_token"])
        participant_tokens[p.participant_id] = p.gossip_token
        participants_data.append({
            "participant_id": p.participant_id,
            "role": p.role,
            "ip": p.ip or "-",
            "org": p.org or f"org_{p.participant_id.replace('-', '_')}"
        })

    def resolve_participant_token(folder_name):
        if folder_name in participant_tokens:
            return participant_tokens[folder_name]

        normalized = folder_name.replace("_", "-").lower()
        for participant_id, token in participant_tokens.items():
            if participant_id.lower() == normalized:
                return token
        return ""
    participants_json = json.dumps(participants_data, indent=2)

    client_admin_map = {}
    client_server_map = {}
    try:
        if (abs_base_prod_path / ".client_admin_map.json").exists():
            client_admin_map = json.loads((abs_base_prod_path / ".client_admin_map.json").read_text())
        if (abs_base_prod_path / ".client_server_map.json").exists():
            client_server_map = json.loads((abs_base_prod_path / ".client_server_map.json").read_text())
    except Exception: pass

    server_dir_names = {"server"}
    server_dir_names.update({str(s) for s in client_server_map.values() if s})

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as main_zip:
        # 1. Add Metadata to main bundle
        main_zip.writestr(".network_id", str(swarm_network.identifier))
        main_zip.writestr(".participants.json", participants_json)
        if swarm_network.gossip_token:
            main_zip.writestr(".gossip_token", swarm_network.gossip_token)

        # 2. Add individual kits
        for item in os.scandir(str(abs_base_prod_path)):
            if item.is_dir() and item.name not in server_dir_names and "admin" not in item.name:
                startup_dir = Path(item.path) / "startup"
                if not startup_dir.exists(): continue

                client_zip_buffer = io.BytesIO()
                with zipfile.ZipFile(client_zip_buffer, "w", zipfile.ZIP_DEFLATED) as client_zip:
                    client_zip.writestr(".network_id", str(swarm_network.identifier))
                    if swarm_network.gossip_token:
                        client_zip.writestr(".gossip_token", swarm_network.gossip_token)
                    client_zip.writestr(".participants.json", participants_json)
                    
                    # Requirements
                    req_file = abs_base_prod_path.parent.parent / "runtime_requirements.txt"
                    if req_file.exists():
                        client_zip.write(str(req_file), "runtime_requirements.txt")

                    # Kit Files
                    for root, _, files in os.walk(item.path):
                        for file in files:
                            file_path = Path(root) / file
                            client_zip.write(str(file_path), str(file_path.relative_to(item.path)))

                    # Admin and Server partials
                    mapped_admin = client_admin_map.get(item.name, "")
                    adm_dir = abs_base_prod_path / mapped_admin / "startup" if mapped_admin else (abs_base_prod_path / "admin@nvidia.com" / "startup")
                    if adm_dir.exists():
                        for root, _, files in os.walk(str(adm_dir)):
                            for file in files:
                                f_path = Path(root) / file
                                client_zip.write(str(f_path), str(Path("admin_startup") / f_path.relative_to(adm_dir)))

                    mapped_srv = client_server_map.get(item.name, "")
                    srv_dir = abs_base_prod_path / mapped_srv / "startup" if mapped_srv else (abs_base_prod_path / "server" / "startup")
                    if srv_dir.exists():
                        for root, _, files in os.walk(str(srv_dir)):
                            for file in files:
                                f_path = Path(root) / file
                                client_zip.write(str(f_path), str(Path("server_startup") / f_path.relative_to(srv_dir)))

                main_zip.writestr(f"{item.name}.zip", client_zip_buffer.getvalue())

        # Root project files
        project_yml = abs_base_prod_path.parent.parent / "project.yml"
        if project_yml.exists(): main_zip.write(str(project_yml), "project.yml")
        
        req_root = abs_base_prod_path.parent.parent / "runtime_requirements.txt"
        if req_root.exists(): main_zip.write(str(req_root), "runtime_requirements.txt")

    zip_buffer.seek(0)
    return zip_buffer
