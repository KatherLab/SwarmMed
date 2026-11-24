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
After a switch the Hub's admin session connects only to the recorded server,
unless this Hub started that server (see :func:`admin_session_pin`).

Every FL server that SwarmMed starts, the standby included, also gets the admin
timeout set in ``SWARMMEDHUB_FLARE_ADMIN_TIMEOUT`` (see
:func:`apply_flare_admin_timeout`).
"""

from __future__ import annotations

import copy
import ipaddress
import json
import math
import os
import re
import shutil
from pathlib import Path

import yaml
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify

ACTIVE_SERVER_FILE = ".active_server.json"

# The server's local resources as NVFlare 2.7.1 provisioning writes them. Used
# when a startup bundle predates the copy of the server's local folder, and by
# set_server_admin_timeout when a server's local folder has no resources file.
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
    address. Nor are leading zeros, which the C library reads as octal
    (``010.0.0.1`` is not 10.0.0.1 there), or digits other than ASCII ones.
    """
    try:
        ipaddress.IPv4Address(str(host or "").strip())
    except ValueError:
        return False
    return True


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


def active_server_record_for_startup(startup_dir) -> dict:
    """Return the switch record that applies to a kit's startup folder, or {}.

    ``startup_dir`` is ``prod_00/startup``, ``prod_00/<participant>/startup``
    (site, server and admin kits) or, for the admin kit of a single site's kit
    imported on its own, ``prod_00/admin_startup/startup``.
    """
    if not startup_dir:
        return {}
    startup = Path(str(startup_dir)).absolute()
    for prod_dir in list(startup.parents)[:3]:
        # Kits always live in a provisioning folder such as prod_00.
        if not prod_dir.name.startswith("prod_"):
            continue
        record = _read_record(prod_dir)
        if str(record.get("server_host") or "").strip():
            return record
    return {}


def active_server_host_for_startup(startup_dir) -> str:
    """Return the switched-to server host for a kit's startup folder, or ''.

    See :func:`active_server_record_for_startup` for the folders.
    """
    record = active_server_record_for_startup(startup_dir)
    return str(record.get("server_host") or "").strip()


def admin_session_pin(record: dict) -> str:
    """Return the only address the Hub's admin session may use after a switch.

    The admin session submits and monitors the network's jobs. Without a switch
    it tries the kit's server name ``server`` first, which inside the Hub's
    containers reaches an FL server container of the network at this Hub if one
    runs, then the configured address and local addresses. After a switch such
    a server is a stale one, for example the coordinator's server started again
    or one that a site started without client-only mode, so the session connects
    only to the recorded server. The standby that started the recorded server
    runs it here and keeps the local addresses; the record says so in
    ``server_started_here``, which every record since version 1.0.3 has.

    Args:
        record: The network's switch record ({} if there is none).

    Returns:
        str: The recorded server address, or '' without a switch and at the Hub
        that started the recorded server.
    """
    host = str(record.get("server_host") or "").strip()
    if not host or record.get("server_started_here") is True:
        return ""
    return host


def active_server_record(network) -> dict:
    """Return the network's switch record, or {} when it uses its provisioned server."""
    return _read_record(network_prod_dir(network))


def server_start_refusal(network) -> str:
    """Return why 'swarmed network start' must not start this Hub's FL server.

    After a switch to a server that this Hub did not start (see
    :func:`admin_session_pin`), the network's sites use that server, so a
    server started here too would be a second server for the network. Starts
    with an explicit role, which ``swarmed network failover`` makes, are not
    checked; nor are local test networks.

    Returns:
        str: The reason, or '' when nothing speaks against the start.
    """
    if network.creation_method == "LOCAL_TEST":
        return ""
    record = active_server_record(network)
    host = admin_session_pin(record)
    if not host:
        return ""
    return (
        f"This Hub uses the FL server at {host} (switched at "
        f"{record.get('changed_at') or 'an unknown time'}), so 'swarmed network "
        "start' does not start this Hub's own FL server: two FL servers must never "
        "run for one network. To start only this Hub's FL clients, set "
        "SWARMMEDHUB_CLIENT_ONLY_MODE for the worker. To run the server here "
        "again, follow 'To return to the coordinating server later' in "
        "docs/failover.md."
    )


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


def unswitched_server_host(network) -> str:
    """Return the server address this Hub uses for a network without a switch.

    As for the FL clients: ``SWARMMEDHUB_SERVER_HOST``, else the address in the
    kits' ``server_host.txt``.
    """
    env_host = os.getenv("SWARMMEDHUB_SERVER_HOST", "").strip()
    return env_host or provisioned_server_host(network)


def previous_server_host(network) -> str:
    """Return the server this Hub used for a network before its recorded switch.

    The record names it in ``previous_server_host``. Where it does not, or names
    the server switched to, the address the Hub uses without a switch is taken
    (:func:`unswitched_server_host`).

    Returns:
        str: The address, or '' without a switch or when it is not known.
    """
    record = active_server_record(network)
    current = str(record.get("server_host") or "").strip()
    if not current:
        return ""
    for host in (record.get("previous_server_host"), unswitched_server_host(network)):
        host = str(host or "").strip()
        if host and host != current:
            return host
    return ""


def write_active_server(
    network, server_host: str, *, started_server: bool, actor=None
) -> dict:
    """Record that a network's sites at this Hub now use ``server_host``.

    The record keeps the server used before (``previous_server_host``), which
    ``swarmed training resume`` asks whether it still runs the interrupted job.
    A repeated switch to the same server keeps the server used before that.
    """
    server_host = str(server_host or "").strip()
    if not is_valid_server_host(server_host):
        raise ValueError(f"Not an IPv4 address: {server_host!r}")
    prod_dir = network_prod_dir(network)
    if not prod_dir.is_dir():
        raise FileNotFoundError(
            f"No startup kits for network '{network.name}' at this Hub ({prod_dir})."
        )
    previous = _read_record(prod_dir)
    previous_host = str(previous.get("server_host") or "").strip()
    if previous_host == server_host:
        previous_host = str(previous.get("previous_server_host") or "").strip()
    record = {
        "server_host": server_host,
        "previous_server_host": previous_host or unswitched_server_host(network),
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


# The FL server waits admin_timeout seconds for each site's reply to an admin
# request, such as deploying a job; provisioning sets 10 s. In a test across three
# hospitals, a resumed job, which carries its checkpoint, did not reach the starting
# site within 10 s over a slow link: the server started the job without that site,
# and the job failed. 300 s fixed it.
FLARE_ADMIN_TIMEOUT_ENV = "SWARMMEDHUB_FLARE_ADMIN_TIMEOUT"


def flare_admin_timeout_from_env(logger=None) -> float | None:
    """Return the FL server's admin timeout set in SWARMMEDHUB_FLARE_ADMIN_TIMEOUT.

    Args:
        logger: Optional platform logger; receives a warning for a value that is
            not a positive number of seconds.

    Returns:
        float | None: Seconds, or None when the variable is unset or invalid.
    """
    raw = os.getenv(FLARE_ADMIN_TIMEOUT_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        if logger is not None:
            logger.network.warning(
                f"Ignoring {FLARE_ADMIN_TIMEOUT_ENV}={raw!r}: "
                "expected a positive number of seconds"
            )
        return None
    return value


def set_server_admin_timeout(server_workspace, admin_timeout: float) -> bool:
    """Set ``admin_timeout`` in an FL server workspace's ``local/resources.json``.

    FLARE reads ``local/resources.json`` before ``local/resources.json.default``.
    The new content starts from the first of these that exists: the file itself,
    the ``.default`` file that provisioning writes, or _SERVER_RESOURCES. Only
    ``servers[0]["admin_timeout"]`` changes. The server reads the file when it
    starts.

    Args:
        server_workspace: The server's FLARE workspace, such as ``prod_00/server``.
        admin_timeout: Seconds.

    Returns:
        bool: True if ``local/resources.json`` was written; False if the file
        FLARE reads already has the value.

    Raises:
        OSError: If a file cannot be read or written.
        ValueError: If the file to start from is not a FLARE resources file.
    """
    local_dir = Path(server_workspace) / "local"
    target = local_dir / "resources.json"
    source = next(
        (
            path
            for path in (target, local_dir / "resources.json.default")
            if path.exists()
        ),
        None,
    )
    if source is None:
        resources = copy.deepcopy(_SERVER_RESOURCES)
    else:
        resources = json.loads(source.read_text())
        if not isinstance(resources, dict):
            raise ValueError(f"{source} does not hold a JSON object")
    servers = resources.setdefault("servers", [])
    if not isinstance(servers, list) or not all(
        isinstance(server, dict) for server in servers
    ):
        raise ValueError(f"'servers' in {source} is not a list of objects")
    if not servers:
        servers.append({})
    if source is not None and servers[0].get("admin_timeout") == admin_timeout:
        return False
    servers[0]["admin_timeout"] = admin_timeout
    local_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(resources, indent=2) + "\n")
    return True


def apply_flare_admin_timeout(server_workspaces, logger=None) -> list[str]:
    """Write SWARMMEDHUB_FLARE_ADMIN_TIMEOUT into an FL server's local settings.

    Called before the server starts, which is when the value takes effect.
    Nothing is written when the variable is unset or invalid; an invalid value
    is logged as a warning. A path without a server kit is skipped, which
    leaves client workspaces alone, as well as a Docker host's path that does
    not exist where the Hub runs, or that the Hub cannot read.

    Args:
        server_workspaces: The server's workspace as the Hub sees it and, if
            different, as the Docker host sees it.
        logger: Optional platform logger.

    Returns:
        list[str]: The files that were written.
    """
    admin_timeout = flare_admin_timeout_from_env(logger)
    if admin_timeout is None:
        return []
    written = []
    for workspace in dict.fromkeys(Path(path).absolute() for path in server_workspaces):
        # os.path, not pathlib: Path.is_file() raises PermissionError (Python
        # 3.12) for a path under a folder this process cannot enter, such as the
        # Docker host's /root seen from the worker, which runs as appuser.
        if not os.path.isfile(workspace / "startup" / "fed_server.json"):
            continue
        resources = workspace / "local" / "resources.json"
        try:
            changed = set_server_admin_timeout(workspace, admin_timeout)
        except (OSError, ValueError) as exc:
            if logger is not None:
                logger.network.warning(
                    f"Could not set admin_timeout in {resources}; the FL server "
                    f"keeps its current value: {exc}"
                )
            continue
        if changed:
            written.append(str(resources))
            if logger is not None:
                logger.network.info(
                    f"Set the FL server's admin_timeout to {admin_timeout:g} s in "
                    f"{resources} ({FLARE_ADMIN_TIMEOUT_ENV})."
                )
    return written


# A site's kit in a startup package also carries the server's kit, so that the
# site can host a standby server, and the site's admin kit. They must not stay in
# the site's FLARE workspace (prod_00/<site>): FLARE looks up configuration files
# such as comm_config.json and resources.json by searching the whole workspace
# and takes the first match (nvflare.fuel.utils.config_factory). With the
# server's comm_config.json, a client's job process looked for its parent
# process at the FL server and the client never trained.
SERVER_KIT_FOLDERS = {"server_startup": "startup", "server_local": "local"}
ADMIN_KIT_FOLDER = "admin_startup"


def bundled_kit_destinations(workspace, prod_dir, participant_name) -> dict[str, Path]:
    """Return where the folders a site's kit carries for other roles belong.

    Args:
        workspace: The site's FLARE workspace, ``prod_00/<participant>``, or
            ``prod_00`` itself when a single site's kit was imported on its own.
        prod_dir: The network's ``prod_00`` folder.
        participant_name: The site's participant name.

    Returns:
        dict[str, Path]: Folder name in the kit -> where the folder belongs,
        outside the site's workspace. The server's kit belongs in
        ``prod_00/server``, where the standby server is started from, and the
        admin kit in ``prod_00/admin-<participant>@nvidia.com/startup``.
    """
    prod_dir = Path(os.path.abspath(prod_dir))
    if Path(os.path.abspath(workspace)) == prod_dir:
        # prod_00 is the site's workspace, so the server's kit stays in the
        # network's folder (<network>/workspace/<study>/prod_00 -> <network>).
        # The admin kit stays in prod_00/admin_startup, where the Hub uses it;
        # of its files, a site's configuration search can find only the study's
        # root certificate, which is the same in every kit.
        return {name: prod_dir.parents[2] / name for name in SERVER_KIT_FOLDERS}
    destinations = {
        name: prod_dir / "server" / folder
        for name, folder in SERVER_KIT_FOLDERS.items()
    }
    destinations[ADMIN_KIT_FOLDER] = (
        prod_dir / f"admin-{participant_name}@nvidia.com" / "startup"
    )
    return destinations


def _copy_if_missing(source, destination):
    if not os.path.lexists(destination):
        shutil.copy2(source, destination)
    return destination


def move_kit_folder(source, destination, *, replace_existing: bool) -> None:
    """Move a kit folder to ``destination``, merging it into a copy already there.

    Args:
        source: The folder to move. It is removed only once each of its files
            has a file of the same name at the destination.
        destination: Where the folder belongs.
        replace_existing: Whether the files of ``source`` replace files of the
            same name at ``destination``. An import replaces them; repairing an
            earlier import keeps the copy that is in use.

    Raises:
        OSError: If a file could not be copied; ``source`` is then kept.
    """
    source = Path(source)
    destination = Path(destination)
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        copy_function=shutil.copy2 if replace_existing else _copy_if_missing,
    )
    shutil.rmtree(source)
