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
from .fingerprint import MANIFEST_NAME, build_manifest, canonical_bytes
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
    The folders above ``path`` inside ``workspaces/``, such as the project's
    folder that the first network of a project creates, are handed over too,
    but not what else they hold.
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

    workspaces_root = (Path(settings.BASE_DIR) / "workspaces").absolute()
    for parent in root.absolute().parents:
        if parent == workspaces_root or workspaces_root not in parent.parents:
            break
        _adjust(parent)

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


# Every entry of an exported package gets this time stamp and one of two modes, so
# that exporting the same network twice gives the same bytes.
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _zip_entry(name: str, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3  # Unix, so that the permission bits below apply
    info.external_attr = (0o100755 if executable else 0o100644) << 16
    return info


def _reproducible_zip(entries: dict[str, tuple[bytes, bool]]) -> bytes:
    """Return a ZIP of ``{name: (content, executable)}`` with entries in name order."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in sorted(entries):
            content, executable = entries[name]
            archive.writestr(_zip_entry(name, executable), content)
    return buffer.getvalue()


def _read_kit_file(path: Path) -> tuple[bytes, bool]:
    """Return a file's content, and whether its owner may execute it."""
    return path.read_bytes(), bool(path.stat().st_mode & stat.S_IXUSR)


def create_startup_kits_zip(swarm_network):
    """Gathers all client startup kits and packages them into a single ZIP file.

    The package holds the network's metadata (``.network_id``,
    ``.participants.json``, ``.gossip_token``), ``project.yml`` and
    ``runtime_requirements.txt``, one ``<site>.zip`` per client site, and
    ``manifest.json``, which is also written into every site's kit; its SHA-256
    is the network fingerprint (see ``network.fingerprint``). Entries are sorted
    and get a fixed time stamp and mode, so that exporting the same network twice
    gives the same bytes.

    Args:
        swarm_network (SwarmNetwork): The swarm network instance.

    Returns:
        io.BytesIO: A buffer containing the ZIP file data; empty if this Hub holds
        no site's startup kit for the network, as at a Hub that imported a
        single site's kit.

    Raises:
        PackageVerificationError: If the kits disagree with each other or with the
            network's SERVER participant, for example about the FL server's
            address.
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
    # Provisioning writes project.yml and runtime_requirements.txt into the
    # network's folder, <network>/, which holds workspace/<study>/prod_00.
    network_dir = abs_base_prod_path.parents[2]

    # Participant Metadata, in a defined order
    participants_data = []
    participant_tokens = {}
    for p in swarm_network.participants.all().order_by("role", "participant_id"):
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

    # 1. Metadata, in the package and in every site's kit
    metadata = {
        ".network_id": str(swarm_network.identifier).encode(),
        ".participants.json": participants_json.encode(),
    }
    if swarm_network.gossip_token:
        metadata[".gossip_token"] = swarm_network.gossip_token.encode()
    req_file = network_dir / "runtime_requirements.txt"
    requirements = req_file.read_bytes() if req_file.is_file() else None

    # 2. Individual kits, as {path: (content, executable)}
    kits: dict[str, dict[str, tuple[bytes, bool]]] = {}
    for item in sorted(os.scandir(str(abs_base_prod_path)), key=lambda entry: entry.name):
        if item.is_dir() and item.name not in server_dir_names and "admin" not in item.name:
            startup_dir = Path(item.path) / "startup"
            if not startup_dir.exists(): continue

            files = {name: (content, False) for name, content in metadata.items()}
            if requirements is not None:
                files["runtime_requirements.txt"] = (requirements, False)

            # Kit Files, each name once. An import also extracts the
            # metadata above into the site's folder, and earlier imports
            # left the other roles' kits in it too; those are added below
            # from where the Hub keeps them. The kit's manifest.json is
            # written below; one found here is not a file of the kit.
            for root, dirs, filenames in os.walk(item.path):
                if root == item.path:
                    dirs[:] = [d for d in dirs if d not in other_role_kits]
                for file in filenames:
                    file_path = Path(root) / file
                    arcname = file_path.relative_to(item.path).as_posix()
                    if arcname not in files and arcname != MANIFEST_NAME:
                        files[arcname] = _read_kit_file(file_path)

            # Admin and Server partials
            mapped_admin = client_admin_map.get(item.name, "")
            site_admin = f"admin-{item.name}@nvidia.com"
            if not mapped_admin and (abs_base_prod_path / site_admin).is_dir():
                # Where an imported package keeps the site's admin kit.
                mapped_admin = site_admin
            adm_dir = abs_base_prod_path / mapped_admin / "startup" if mapped_admin else (abs_base_prod_path / "admin@nvidia.com" / "startup")
            if adm_dir.exists():
                for root, _, filenames in os.walk(str(adm_dir)):
                    for file in filenames:
                        f_path = Path(root) / file
                        files[(Path("admin_startup") / f_path.relative_to(adm_dir)).as_posix()] = _read_kit_file(f_path)

            mapped_srv = client_server_map.get(item.name, "")
            srv_dir = abs_base_prod_path / mapped_srv / "startup" if mapped_srv else (abs_base_prod_path / "server" / "startup")
            if srv_dir.exists():
                for root, _, filenames in os.walk(str(srv_dir)):
                    for file in filenames:
                        f_path = Path(root) / file
                        files[(Path("server_startup") / f_path.relative_to(srv_dir)).as_posix()] = _read_kit_file(f_path)

            # The server's local settings (job store, scheduler, launcher;
            # no keys), so that the site can host the server as a standby.
            srv_local_dir = srv_dir.parent / "local"
            if srv_local_dir.is_dir():
                for root, _, filenames in os.walk(str(srv_local_dir)):
                    for file in filenames:
                        f_path = Path(root) / file
                        files[(Path("server_local") / f_path.relative_to(srv_local_dir)).as_posix()] = _read_kit_file(f_path)

            kits[item.name] = files

    if not kits:
        # No site's kit is here, for example at a Hub that imported a single
        # site's kit (prod_00 is that kit), so there is no package to export.
        return zip_buffer

    # 3. Root project files
    top_files = dict(metadata)
    project_yml = network_dir / "project.yml"
    if project_yml.is_file():
        top_files["project.yml"] = project_yml.read_bytes()
    if requirements is not None:
        top_files["runtime_requirements.txt"] = requirements

    # 4. The manifest, in the package and in every site's kit
    manifest = canonical_bytes(
        build_manifest(
            network_id=str(swarm_network.identifier),
            project=project_name,
            top_files=top_files,
            kits={
                site: {path: content for path, (content, _) in files.items()}
                for site, files in kits.items()
            },
        )
    )
    entries = {name: (content, False) for name, content in top_files.items()}
    entries[MANIFEST_NAME] = (manifest, False)
    for site, files in kits.items():
        entries[f"{site}.zip"] = (
            _reproducible_zip({**files, MANIFEST_NAME: (manifest, False)}),
            False,
        )
    zip_buffer.write(_reproducible_zip(entries))
    zip_buffer.seek(0)
    return zip_buffer
