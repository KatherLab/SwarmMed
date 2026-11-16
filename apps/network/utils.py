"""Utility functions for the network app.
Provides machine identification, packaging of startup kits into zip files,
and the old names of the overlay-address helpers, which now live in
``network.overlay``.
"""

import io
import json
import os
import pwd
import random
import stat
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.utils.text import slugify

from .failover import ADMIN_KIT_FOLDER, SERVER_KIT_FOLDERS
from .overlay import get_overlay_ip, is_overlay_connected

# Old location of the status file, written by the old scripts/tailscale_watcher.sh.
# network.overlay still reads it (computing the path at call time).
TAILSCALE_STATUS_FILE = Path(settings.BASE_DIR) / "tmp" / "tailscale_status.json"


def _runtime_service_identity() -> tuple[int, int]:
    """Return the UID/GID that long-running app workers use."""
    user_name = os.getenv("SWARMMEDHUB_RUNTIME_USER", "appuser")
    try:
        user = pwd.getpwnam(user_name)
        return user.pw_uid, user.pw_gid
    except KeyError:
        return os.getuid(), os.getgid()


def ensure_worker_writable(path: str | os.PathLike, logger=None) -> None:
    """Make a generated workspace writable by the app worker process.

    CLI invocations run through ``docker compose exec`` often start as root,
    while gunicorn/celery run as ``appuser``. Provisioned startup kits must be
    handed back to that worker identity before asynchronous start tasks run.
    """
    root = Path(path)
    if not root.exists():
        return

    uid, gid = _runtime_service_identity()
    failures: list[str] = []

    def _adjust(item: Path) -> None:
        try:
            if os.geteuid() == 0:
                os.chown(item, uid, gid, follow_symlinks=False)
            if item.is_symlink():
                return
            current_mode = item.stat().st_mode
            desired_mode = current_mode | stat.S_IRUSR | stat.S_IWUSR
            if item.is_dir():
                desired_mode |= stat.S_IXUSR
            os.chmod(item, stat.S_IMODE(desired_mode))
        except OSError as exc:
            failures.append(f"{item}: {exc}")

    _adjust(root)
    for current_root, dirs, files in os.walk(root):
        for name in dirs:
            _adjust(Path(current_root) / name)
        for name in files:
            _adjust(Path(current_root) / name)

    if failures and logger:
        logger.network.warning(
            f"Could not normalize workspace permissions for {root}: "
            f"{'; '.join(failures[:5])}"
        )


def _safe_cache_get(key):
    try:
        return cache.get(key)
    except Exception:
        return None


def _safe_cache_set(key, value, timeout):
    try:
        cache.set(key, value, timeout)
    except Exception:
        pass


def _read_tailscale_file():
    """Reads the old helper's status file (kept for scripts that import it).

    Returns:
        dict: The status data or None if unavailable.
    """
    if not TAILSCALE_STATUS_FILE.exists():
        return None
    try:
        with open(TAILSCALE_STATUS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def get_tailscale_ip():
    """Old name of ``network.overlay.get_overlay_ip``.

    Returns this host's overlay address (NetBird or Tailscale, as selected by
    SWARMMEDHUB_OVERLAY), or 'Not Available'.
    """
    return get_overlay_ip()


def is_tailscale_connected():
    """Old name of ``network.overlay.is_overlay_connected``.

    Returns "connected", "disconnected" or "Not Available"; a stale status
    counts as "disconnected", as this function never returned "stale".
    """
    state = is_overlay_connected()
    return "disconnected" if state == "stale" else state


def get_hostname():
    """Returns a unique, human-friendly hostname for the current machine.
    
    1. Checks environment variable SWARMMEDHUB_HOSTNAME.
    2. Checks for a persisted hostname in a local file.
    3. Generates and persists a new random human-friendly name if none exists.

    Returns:
        str: The human-friendly hostname.
    """
    # 1. Environment variable override
    env_hostname = os.environ.get("SWARMMEDHUB_HOSTNAME")
    if env_hostname:
        return env_hostname

    # Path to the persisted hostname file
    hostname_file = Path(settings.BASE_DIR) / ".swarmmedhub_hostname"

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
    # The folders of a site's kit that hold the server's and the admin's kits.
    other_role_kits = {*SERVER_KIT_FOLDERS, ADMIN_KIT_FOLDER}

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

                    # Kit Files, each name once. An import also extracts the
                    # metadata above into the site's folder, and earlier imports
                    # left the other roles' kits in it too; those are added below
                    # from where the Hub keeps them.
                    written = set(client_zip.namelist())
                    for root, dirs, files in os.walk(item.path):
                        if root == item.path:
                            dirs[:] = [d for d in dirs if d not in other_role_kits]
                        for file in files:
                            file_path = Path(root) / file
                            arcname = str(file_path.relative_to(item.path))
                            if arcname not in written:
                                client_zip.write(str(file_path), arcname)

                    # Admin and Server partials
                    mapped_admin = client_admin_map.get(item.name, "")
                    site_admin = f"admin-{item.name}@nvidia.com"
                    if not mapped_admin and (abs_base_prod_path / site_admin).is_dir():
                        # Where an imported package keeps the site's admin kit.
                        mapped_admin = site_admin
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

                    # The server's local settings (job store, scheduler, launcher;
                    # no keys), so that the site can host the server as a standby.
                    srv_local_dir = srv_dir.parent / "local"
                    if srv_local_dir.is_dir():
                        for root, _, files in os.walk(str(srv_local_dir)):
                            for file in files:
                                f_path = Path(root) / file
                                client_zip.write(str(f_path), str(Path("server_local") / f_path.relative_to(srv_local_dir)))

                main_zip.writestr(f"{item.name}.zip", client_zip_buffer.getvalue())

        # Root project files
        project_yml = abs_base_prod_path.parent.parent / "project.yml"
        if project_yml.exists(): main_zip.write(str(project_yml), "project.yml")
        
        req_root = abs_base_prod_path.parent.parent / "runtime_requirements.txt"
        if req_root.exists(): main_zip.write(str(req_root), "runtime_requirements.txt")

    zip_buffer.seek(0)
    return zip_buffer
