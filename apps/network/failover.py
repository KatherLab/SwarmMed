"""Point a site at another FL server, and start a standby server from its kit.

NVIDIA FLARE runs one FL server per project. Its own failover support (one
active server, hot standbys and an overseer) is not available in the FLARE
2.7.1 that SwarmMed runs, and it never kept a swarm job running across a
switch. SwarmMed offers a cold standby instead. Every site's startup bundle
already contains the server's kit, so a site agreed in advance can start the
server when the coordinating host is lost. Every site then points its FL
client and its Hub at that host. The job that was running does not survive the
switch;
``training.resume`` starts a new job from the newest checkpoint.

The switch is recorded per network in ``prod_00/.active_server.json``. That
file takes precedence over ``SWARMMEDHUB_SERVER_HOST`` and over the kits'
``server_host.txt``, which keep the address the network was provisioned with.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

ACTIVE_SERVER_FILE = ".active_server.json"

_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")

# The server's local resources as NVFlare 2.7.1 provisioning writes them. Used
# only when a startup bundle predates the copy of the server's local folder.
_SERVER_RESOURCES = {
    "format_version": 2,
    "servers": [
        {
            "admin_storage": "transfer",
            "max_num_clients": 100,
            "heart_beat_timeout": 600,
            "download_job_url": "http://download.server.com/",
            "admin_timeout": 10.0,
        }
    ],
    "snapshot_persistor": {
        "path": "nvflare.app_common.state_persistors.storage_state_persistor.StorageStatePersistor",  # noqa: E501
        "args": {
            "uri_root": "/",
            "storage": {
                "path": "nvflare.app_common.storages.filesystem_storage.FilesystemStorage",  # noqa: E501
                "args": {
                    "root_dir": "/tmp/nvflare/snapshot-storage",  # nosec B108
                    "uri_root": "/",
                },
            },
        },
    },
    "components": [
        {
            "id": "job_scheduler",
            "path": "nvflare.app_common.job_schedulers.job_scheduler.DefaultJobScheduler",  # noqa: E501
            "args": {"max_jobs": 4},
        },
        {
            "id": "job_manager",
            "path": "nvflare.apis.impl.job_def_manager.SimpleJobDefManager",
            "args": {
                "uri_root": "/tmp/nvflare/jobs-storage",  # nosec B108
                "job_store_id": "job_store",
            },
        },
        {
            "id": "job_store",
            "path": "nvflare.app_common.storages.filesystem_storage.FilesystemStorage",
        },
        {
            "id": "process_launcher",
            "path": "nvflare.app_common.job_launcher.server_process_launcher.ServerProcessJobLauncher",  # noqa: E501
            "args": {},
        },
        {
            "id": "log_receiver",
            "path": "nvflare.app_common.logging.log_receiver.LogReceiver",
            "args": {},
        },
    ],
}

_SERVER_COMM_CONFIG = {
    "allow_adhoc_conns": False,
    "backbone_conn_gen": 2,
    "internal": {
        "scheme": "tcp",
        "resources": {"host": "server", "connection_security": "clear"},
    },
}


def is_valid_server_host(host: str) -> bool:
    """Return True for an IPv4 address.

    Host names are not accepted: the address reaches the FL client containers
    as ``docker run --add-host server:<address>``, which takes only an IP
    address.
    """
    match = _IPV4_RE.match(str(host or "").strip())
    return bool(match) and all(0 <= int(part) <= 255 for part in match.groups())


def network_prod_dir(network) -> Path:
    """Return the ``prod_00`` folder that holds a network's startup kits."""
    project_name = slugify(network.project.title).replace("-", "_")
    return (
        Path(settings.BASE_DIR)
        / "workspaces"
        / str(network.project.identifier)
        / str(network.identifier)
        / "workspace"
        / project_name
        / "prod_00"
    )


def _read_record(prod_dir: Path) -> dict:
    try:
        payload = json.loads((prod_dir / ACTIVE_SERVER_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def active_server_host_for_startup(startup_dir) -> str:
    """Return the switched-to server host for a kit's startup folder, or ''.

    ``startup_dir`` is ``prod_00/startup``, ``prod_00/<participant>/startup`` or,
    for an imported admin kit, ``prod_00/<participant>/admin_startup/startup``.
    """
    if not startup_dir:
        return ""
    startup = Path(str(startup_dir)).absolute()
    for prod_dir in list(startup.parents)[:3]:
        # Kits always live in a provisioning folder such as prod_00.
        if not prod_dir.name.startswith("prod_"):
            continue
        record = _read_record(prod_dir)
        host = str(record.get("server_host") or "").strip()
        if host:
            return host
    return ""


def active_server_record(network) -> dict:
    """Return the network's switch record, or {} when it uses its provisioned server."""
    return _read_record(network_prod_dir(network))


def provisioned_server_host(network) -> str:
    """Return the server address written into the network's kits at provisioning."""
    prod_dir = network_prod_dir(network)
    for host_file in sorted(prod_dir.glob("*/startup/server_host.txt")) + [
        prod_dir / "startup" / "server_host.txt"
    ]:
        try:
            host = host_file.read_text().strip()
        except OSError:
            continue
        if host:
            return host
    return ""


def write_active_server(
    network, server_host: str, *, started_server: bool, actor=None
) -> dict:
    """Record that a network's sites at this Hub now use ``server_host``."""
    server_host = str(server_host or "").strip()
    if not is_valid_server_host(server_host):
        raise ValueError(f"Not an IPv4 address: {server_host!r}")
    prod_dir = network_prod_dir(network)
    if not prod_dir.is_dir():
        raise FileNotFoundError(
            f"No startup kits for network '{network.name}' at this Hub ({prod_dir})."
        )
    previous = _read_record(prod_dir)
    record = {
        "server_host": server_host,
        "previous_server_host": str(
            previous.get("server_host") or provisioned_server_host(network) or ""
        ),
        "provisioned_server_host": provisioned_server_host(network),
        "server_started_here": bool(started_server),
        "changed_at": timezone.now().isoformat(),
        "changed_by": getattr(actor, "username", "") or "",
    }
    (prod_dir / ACTIVE_SERVER_FILE).write_text(json.dumps(record, indent=2))
    return record


def restore_active_server(network, previous_record: dict) -> None:
    """Put back the switch record a failed switch replaced.

    Args:
        network: The network.
        previous_record: The record before the switch; {} if there was none.
    """
    if previous_record:
        (network_prod_dir(network) / ACTIVE_SERVER_FILE).write_text(
            json.dumps(previous_record, indent=2)
        )
    else:
        clear_active_server(network)


def clear_active_server(network) -> bool:
    """Go back to the provisioned server address.

    Returns:
        bool: True if a switch was undone.
    """
    record_path = network_prod_dir(network) / ACTIVE_SERVER_FILE
    if record_path.exists():
        record_path.unlink()
        return True
    return False


def _default_log_config() -> str:
    """Return FLARE's default logging configuration, as provisioning writes it."""
    try:
        import nvflare.fuel.utils as fuel_utils

        return (Path(fuel_utils.__file__).with_name("log_config.json")).read_text()
    except (ImportError, OSError):
        return ""


def _template_block(name: str) -> str:
    template_path = Path(__file__).with_name("master_template.yml")
    try:
        template = yaml.safe_load(template_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return ""
    return str(template.get(name) or "")


def ensure_server_kit_complete(prod_dir) -> list[str]:
    """Make sure the server kit in an imported bundle can be started here.

    Bundles carry the server's ``startup`` folder. Bundles made before this
    release lack its ``local`` folder (logging, job store, scheduler and
    launcher settings), without which the server does not start or cannot run
    jobs. Each missing file is written with the default that NVFlare 2.7.1
    provisioning produces.

    Returns:
        list[str]: Files that were written.
    """
    server_dir = Path(prod_dir) / "server"
    if not (server_dir / "startup" / "fed_server.json").exists():
        raise FileNotFoundError(
            "This site's startup package has no FL server kit "
            f"({server_dir / 'startup' / 'fed_server.json'}); "
            "it cannot host the server."
        )
    local_dir = server_dir / "local"
    local_dir.mkdir(parents=True, exist_ok=True)
    defaults = {
        "resources.json.default": json.dumps(_SERVER_RESOURCES, indent=2),
        "log_config.json.default": _default_log_config(),
        "comm_config.json": json.dumps(_SERVER_COMM_CONFIG, indent=2),
        "authorization.json.default": _template_block("default_authz"),
        "privacy.json.sample": _template_block("sample_privacy"),
    }
    written: list[str] = []
    for file_name, content in defaults.items():
        # FLARE reads resources.json before resources.json.default, and so on.
        active_name = re.sub(r"\.(default|sample)$", "", file_name)
        if (local_dir / file_name).exists() or (local_dir / active_name).exists():
            continue
        if not content:
            if file_name == "log_config.json.default":
                raise FileNotFoundError(
                    "The FL server kit has no logging configuration and NVFlare's "
                    "default could not be read; the server would not start."
                )
            continue
        (local_dir / file_name).write_text(content)
        written.append(str(local_dir / file_name))
    (server_dir / "transfer").mkdir(parents=True, exist_ok=True)
    return written
