"""Shared network services used by the UI and CLI."""

from __future__ import annotations

import io
import json
import os
import shutil
import zipfile
import zlib
from pathlib import Path

import yaml
from django.conf import settings
from django.contrib.auth.models import User
from django.utils.text import slugify

from logs.logger import get_logger
from project.models import Project

from .failover import (
    SERVER_KIT_FOLDERS,
    active_server_record,
    bundled_kit_destinations,
    is_valid_server_host,
    move_kit_folder,
    network_prod_dir,
    provisioned_server_host,
    unswitched_server_host,
)
from .fingerprint import (
    MANIFEST_NAME,
    MAX_PACKAGE_BYTES,
    NOT_SITE_KITS,
    PackageCheck,
    PackageVerificationError,
    certificate_fingerprint,
    check_package,
    group_fingerprint,
    normalize_fingerprint,
    open_archive,
    package_fingerprint,
    read_entry,
    sha256_hex,
)
from .models import SwarmNetwork, SwarmParticipant, UserCurrentNetwork
from .overlay import resolve_server_address
from .provision import generate_flare_startup_kit, safe_participant_name
from .tasks import (
    failover_network_task,
    start_swarm_network_task,
    stop_swarm_network_task,
)
from .utils import (
    create_startup_kits_zip,
    ensure_worker_writable,
    get_hostname,
)


def list_project_networks(project: Project):
    """List all networks for a project."""
    return SwarmNetwork.objects.filter(project=project).select_related(
        "project", "author"
    ).prefetch_related("participants").order_by("-created_at")


def get_network_for_project(project: Project, identifier: str) -> SwarmNetwork:
    """Resolve a network by UUID identifier inside a project."""
    network = (
        SwarmNetwork.objects.filter(project=project, identifier=identifier)
        .select_related("project", "author")
        .prefetch_related("participants")
        .first()
    )
    if not network:
        raise LookupError(
            f"Network '{identifier}' was not found for project '{project.title}'."
        )
    return network


def get_current_network(user: User, project: Project | None = None) -> SwarmNetwork | None:
    """Return the user's current network."""
    relation = (
        UserCurrentNetwork.objects.select_related("network__project")
        .filter(user=user)
        .first()
    )
    if not relation or not relation.network:
        return None
    if project is not None and relation.network.project_id != project.id:
        return None
    return relation.network


def set_current_network(user: User, network: SwarmNetwork) -> SwarmNetwork:
    """Set a network as the user's active network."""
    UserCurrentNetwork.objects.update_or_create(
        user=user, defaults={"network": network}
    )
    return network


def _dedupe_clients(clients: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen_names: set[str] = set()
    for client in clients:
        participant_name = str(client.get("name", "")).strip()
        if not participant_name or participant_name in seen_names:
            continue
        deduped.append(client)
        seen_names.add(participant_name)
    return deduped


def create_network(
    *,
    project: Project,
    actor: User,
    name: str,
    description: str = "",
    participants: list[dict] | None = None,
    server_ip: str | None = None,
    include_local_client: bool = True,
) -> SwarmNetwork:
    """Create and provision a real network startup package.

    ``server_ip`` is the FL server's IPv4 address, written into every kit.
    Without it, this host's current overlay address (NetBird or Tailscale) is
    used. Raises ValueError, before anything is created, if the given address
    is not usable or if no address is given and the overlay status is stale,
    absent or disconnected.
    """
    participants = participants or []
    log = get_logger(user=actor, project=project)

    # Resolve the address first, so that a refusal leaves no half-created network.
    resolved_server_ip = resolve_server_address(server_ip)

    swarm_network = SwarmNetwork.objects.create(
        name=name,
        project=project,
        description=description,
        author=actor,
        creation_method="CREATED",
    )
    log.network.info(
        f"Initialized new network record: {name} (ID: {swarm_network.identifier}), "
        f"FL server address {resolved_server_ip}"
    )

    clients = []
    for participant in participants:
        safe_name = safe_participant_name(participant.get("name", "client"))
        if not safe_name:
            continue
        clients.append({"name": safe_name, "ip": participant.get("ip", "")})

    if include_local_client:
        # The local client runs on this host, which is the FL server's host.
        local_client_name = safe_participant_name(get_hostname() or "", default="")
        if local_client_name:
            clients.append({"name": local_client_name, "ip": resolved_server_ip})

    clients = _dedupe_clients(clients)

    SwarmParticipant.objects.create(
        network=swarm_network,
        user=actor,
        role="SERVER",
        participant_id="server",
        org="swarm_control_plane",
        ip=resolved_server_ip,
    )
    for client in clients:
        SwarmParticipant.objects.create(
            network=swarm_network,
            user=actor,
            role="CLIENT",
            participant_id=client["name"],
            org=f"org_{client['name'].replace('-', '_')}",
            ip=client["ip"],
        )

    log.network.info(
        f"Provisioning network '{name}' with {len(clients)} clients.",
        clients=clients,
    )
    generate_flare_startup_kit(
        network_id=swarm_network.identifier,
        local_test=False,
        clients=clients,
        server_ip=resolved_server_ip,
    )
    ensure_worker_writable(
        Path(settings.BASE_DIR)
        / "workspaces"
        / str(project.identifier)
        / str(swarm_network.identifier),
        logger=log,
    )
    return swarm_network


def create_local_test_network(
    *,
    project: Project,
    actor: User,
    name: str,
    description: str = "",
    client_count: int = 2,
) -> SwarmNetwork:
    """Create and provision a local-test network.

    All participants run on this host. ``client_count`` sets how many FL clients
    are provisioned (``fl-client-1`` … ``fl-client-N``); it is used to study how
    the platform behaves as a consortium grows.
    """
    client_count = max(1, int(client_count))
    log = get_logger(user=actor, project=project)
    swarm_network = SwarmNetwork.objects.create(
        name=name,
        project=project,
        description=description,
        author=actor,
        creation_method="LOCAL_TEST",
    )
    SwarmParticipant.objects.create(
        network=swarm_network,
        user=actor,
        role="SERVER",
        participant_id="server",
        org="swarm_control_plane",
        ip="127.0.0.1",
    )
    for index in range(1, client_count + 1):
        SwarmParticipant.objects.create(
            network=swarm_network,
            user=actor,
            role="CLIENT",
            participant_id=f"fl-client-{index}",
            org="nvidia",
            ip="127.0.0.1",
        )
    log.network.info(
        f"Provisioning local testing network '{name}' with {client_count} clients."
    )
    generate_flare_startup_kit(
        network_id=swarm_network.identifier,
        local_test=True,
        clients=[],
        local_test_clients=client_count,
    )
    ensure_worker_writable(
        Path(settings.BASE_DIR)
        / "workspaces"
        / str(project.identifier)
        / str(swarm_network.identifier),
        logger=log,
    )
    return swarm_network


def _reset_package_source(package_source) -> None:
    if hasattr(package_source, "seek"):
        package_source.seek(0)


def _read_package_bytes(package_source) -> bytes:
    """Read a startup package, given as a path or a file object, into memory.

    Raises:
        ValueError: If it cannot be read, or is larger than MAX_PACKAGE_BYTES;
            no more than that is read.
    """
    limit = MAX_PACKAGE_BYTES
    try:
        if isinstance(package_source, (str, os.PathLike)):
            with open(package_source, "rb") as handle:
                package_bytes = handle.read(limit + 1)
        elif hasattr(package_source, "chunks"):
            # An upload (Django's UploadedFile); chunks() starts at the beginning.
            chunks, size = [], 0
            for chunk in package_source.chunks():
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    break
            package_bytes = b"".join(chunks)
        else:
            _reset_package_source(package_source)
            package_bytes = package_source.read(limit + 1)
    except OSError as exc:
        raise ValueError(f"Could not read the startup package: {exc}") from exc
    if len(package_bytes) > limit:
        raise ValueError(
            f"The startup package is larger than the {limit} bytes that an import "
            "accepts. Nothing was imported."
        )
    return package_bytes


def legacy_package_warning(package_sha256: str = "") -> str:
    """The warning for an imported package that has no manifest."""
    checksum = f" ({package_sha256})" if package_sha256 else ""
    return (
        "The startup package has no manifest.json, as packages exported by SwarmMed "
        "1.0.4 or earlier do, so it has no network fingerprint. Verify it by other "
        f"means, for example by comparing its SHA-256{checksum} with the sender's "
        "by phone or video call."
    )


def _same_participant(first: str, second: str) -> bool:
    """Whether two names are of the same site, '_' and '-' and case aside.

    NVIDIA FLARE does not accept '_' in a participant's name, so provisioning
    writes it as '-' (see provision.safe_participant_name).
    """
    return first.replace("_", "-").lower() == second.replace("_", "-").lower()


def import_warnings(network: SwarmNetwork) -> list[str]:
    """Warnings to show after importing ``network``, as import_network returns it."""
    warnings = []
    if network.creation_method == "IMPORTED" and not network.package_fingerprint:
        warnings.append(legacy_package_warning(network.package_sha256))
    site = getattr(network, "package_site", "")
    local = os.getenv("SWARMMEDHUB_LOCAL_PARTICIPANT", "").strip()
    if site and local and not _same_participant(site, local):
        warnings.append(
            f"This is the kit of site {site}, but SWARMMEDHUB_LOCAL_PARTICIPANT "
            f"names {local}; with this kit, this Hub takes part as {site}. Check "
            "that you were sent your site's kit."
        )
    return warnings


def _check_expected_fingerprint(check: PackageCheck, expected: str) -> None:
    """Refuse a package whose fingerprint is not ``expected`` (normalized)."""
    if not check.fingerprint:
        raise ValueError(
            "The startup package has no manifest.json (packages exported by SwarmMed "
            "1.0.4 or earlier have none), so it has no network fingerprint to "
            "compare with the expected one. Nothing was imported."
        )
    if check.fingerprint == expected:
        return
    hint = ""
    root_ca = str(check.manifest["trust"].get("root_ca_sha256") or "")
    if root_ca.replace(":", "").lower() == expected:
        hint = (
            " The expected value is the root CA certificate's fingerprint, not the "
            "network fingerprint."
        )
    raise ValueError(
        "The startup package's network fingerprint is "
        f"{group_fingerprint(check.fingerprint)}, not the expected "
        f"{group_fingerprint(expected)}.{hint} Nothing was imported."
    )


def verify_startup_package(
    package_bytes: bytes, *, expect_fingerprint: str | None = None
) -> PackageCheck:
    """Check a startup package before anything of it is written.

    Raises:
        ValueError: If it is not a readable ZIP file, has unsafe entries or
            entries compressed with another method than deflate, declares more
            than an import accepts (``fingerprint.MAX_ENTRY_BYTES``,
            ``MAX_PACKAGE_BYTES``, ``MAX_ENTRIES``), does not agree with its
            manifest, or does not have the expected fingerprint; also if
            ``expect_fingerprint`` is malformed, an empty value included.
    """
    # An empty value is malformed too, rather than no expectation: a script that
    # passes an unset variable must not import unchecked.
    expected = (
        normalize_fingerprint(expect_fingerprint)
        if expect_fingerprint is not None
        else ""
    )
    try:
        with open_archive(package_bytes, "the package") as package:
            check = check_package(package)
    except PackageVerificationError as exc:
        raise ValueError(
            f"Refused to import the startup package: {exc}. Nothing was imported."
        ) from exc
    except (
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        NotImplementedError,
        RuntimeError,
    ) as exc:
        raise ValueError(
            f"The startup package cannot be read as a ZIP file ({exc}). Nothing was "
            "imported."
        ) from exc
    if expected:
        _check_expected_fingerprint(check, expected)
    return check


def _read_zip_namelist(package_source) -> tuple[str | None, list[str]]:
    original_network_id = None
    with zipfile.ZipFile(package_source, "r") as zip_peek:
        file_list = zip_peek.namelist()
        if ".network_id" in file_list:
            original_network_id = (
                read_entry(zip_peek, ".network_id").decode("utf-8").strip()
            )
        if not original_network_id:
            for name in file_list:
                if name.startswith("workspaces/"):
                    parts = name.split("/")
                    if len(parts) >= 3:
                        original_network_id = parts[2]
                        break
    return original_network_id, file_list


def import_network(
    *,
    project: Project,
    actor: User,
    name: str,
    description: str = "",
    package_source,
    expect_fingerprint: str | None = None,
) -> SwarmNetwork:
    """Import a network from a startup package zip.

    The package, or a single site's kit taken out of it, is read into memory and
    checked before anything is written (see ``network.fingerprint``): unsafe
    entries, such as absolute paths, ``..`` or symbolic links, are refused, as is
    a package that is or unpacks to more than ``MAX_PACKAGE_BYTES`` or that has
    more than ``MAX_ENTRIES`` entries with those of its kits, and a package with
    a ``manifest.json`` must agree with it. A package without one,
    as SwarmMed 1.0.4 and earlier export, is imported as before, without a
    fingerprint (``import_warnings`` then returns a warning).

    Args:
        project: The project to import the network into.
        actor: The importing user.
        name: The network's name at this Hub.
        description: Its description.
        package_source: The package, as a path or a file object.
        expect_fingerprint: The network fingerprint agreed among the sites; spaces,
            colons, dashes and case are ignored. A package with another
            fingerprint, or without one, is refused, and so is an empty value;
            None means no expectation.

    Raises:
        ValueError: If the package cannot be read, is unsafe, does not agree with
            its manifest, has another fingerprint than expected, or holds a
            network that already exists at this Hub. Nothing is written then.
    """
    log = get_logger(user=actor, project=project)
    package_bytes = _read_package_bytes(package_source)
    package_sha256 = sha256_hex(package_bytes)
    try:
        check = verify_startup_package(
            package_bytes, expect_fingerprint=expect_fingerprint
        )
    except ValueError as exc:
        log.network.warning(f"Startup package for network '{name}' refused: {exc}")
        raise
    if check.fingerprint:
        kit = f" (the kit of site {check.site})" if check.site else ""
        log.network.info(
            f"The startup package{kit} agrees with its manifest; network fingerprint "
            f"{group_fingerprint(check.fingerprint)}, package SHA-256 {package_sha256}."
        )
    else:
        log.network.warning(legacy_package_warning(package_sha256))
    # The rest reads the checked copy, not the source again.
    package_source = io.BytesIO(package_bytes)
    original_network_id = None

    try:
        original_network_id, _ = _read_zip_namelist(package_source)
    except Exception as peek_err:
        log.network.warning(
            f"Failed to peek into zip for identifier: {peek_err}"
        )
    finally:
        _reset_package_source(package_source)

    create_args = {
        "name": name,
        "project": project,
        "description": description,
        "author": actor,
        "creation_method": "IMPORTED",
        "package_fingerprint": check.fingerprint,
        "package_sha256": package_sha256,
    }
    if original_network_id:
        try:
            import uuid

            uuid.UUID(original_network_id)
            create_args["identifier"] = original_network_id
            log.network.info(
                f"Recovered original network identifier: {original_network_id}"
            )
        except Exception:
            pass
    identifier = create_args.get("identifier")
    if identifier and SwarmNetwork.objects.filter(identifier=identifier).exists():
        message = (
            f"The network of this startup package ({identifier}) already exists at "
            "this Hub. Delete it first to import the package again, for example to "
            "get the network fingerprint of a network imported from a package "
            "without one. Nothing was imported."
        )
        log.network.warning(f"Startup package for network '{name}' refused: {message}")
        raise ValueError(message)

    swarm_network = SwarmNetwork.objects.create(**create_args)
    # Not stored: the site whose kit was imported on its own, for the messages.
    swarm_network.package_site = check.site
    log.network.info(
        f"Initialized imported network record: {name} (ID: {swarm_network.identifier})"
    )

    provision_dir = os.path.abspath(
        os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(project.identifier),
            str(swarm_network.identifier),
        )
    )
    os.makedirs(provision_dir, exist_ok=True)

    with zipfile.ZipFile(package_source, "r") as zip_ref:
        abs_provision_dir = os.path.abspath(provision_dir)
        for member in zip_ref.infolist():
            member_path = os.path.normpath(member.filename)
            if member_path.startswith("/") or member_path.startswith(".."):
                continue

            target_path = os.path.abspath(
                os.path.join(abs_provision_dir, member_path)
            )
            if not target_path.startswith(os.path.join(abs_provision_dir, "")):
                continue

            if member_path == "runtime_requirements.txt":
                with open(os.path.join(provision_dir, member_path), "wb") as handle:
                    handle.write(read_entry(zip_ref, member))
            else:
                zip_ref.extract(member, provision_dir)

    project_name = slugify(project.title).replace("-", "_")
    prod_00_dir = os.path.join(
        provision_dir, "workspace", project_name, "prod_00"
    )

    client_archives = [
        path
        for path in Path(provision_dir).glob("*.zip")
        if path.name not in NOT_SITE_KITS
    ]
    if client_archives:
        os.makedirs(prod_00_dir, exist_ok=True)
        for archive_path in client_archives:
            participant_name = archive_path.stem
            participant_dir = os.path.join(prod_00_dir, participant_name)
            os.makedirs(participant_dir, exist_ok=True)
            with zipfile.ZipFile(archive_path, "r") as participant_zip:
                abs_participant_dir = os.path.abspath(participant_dir)
                for nested_member in participant_zip.infolist():
                    if nested_member.filename == MANIFEST_NAME:
                        # A copy of the package's manifest, which stays in the
                        # network's folder; the site's folder is its workspace.
                        continue
                    nested_path = os.path.normpath(nested_member.filename)
                    if nested_path.startswith("/") or nested_path.startswith(".."):
                        continue

                    target_path = os.path.abspath(
                        os.path.join(abs_participant_dir, nested_path)
                    )
                    if not target_path.startswith(
                        os.path.join(abs_participant_dir, "")
                    ):
                        continue

                    participant_zip.extract(nested_member, participant_dir)

            # The server's kit (server_startup, and server_local, which older
            # bundles lack) lets this site host the FL server as a standby (see
            # network.failover); admin_startup is the site's admin kit. They
            # are moved, not copied, out of the site's folder: it is the site's
            # FLARE workspace, which FLARE searches for its configuration files.
            for folder, destination in bundled_kit_destinations(
                participant_dir, prod_00_dir, participant_name
            ).items():
                source = os.path.join(participant_dir, folder)
                if os.path.isdir(source):
                    move_kit_folder(source, destination, replace_existing=True)

    if os.path.exists(os.path.join(provision_dir, "startup")):
        os.makedirs(prod_00_dir, exist_ok=True)
        for item in os.listdir(provision_dir):
            if item in {"workspaces", "runtime_requirements.txt", MANIFEST_NAME}:
                continue
            if item in SERVER_KIT_FOLDERS:
                # A single site's kit: prod_00 becomes the site's FLARE
                # workspace, so the server's kit stays in the network's folder.
                continue
            src = os.path.join(provision_dir, item)
            dst = os.path.join(prod_00_dir, item)
            if os.path.abspath(src) == os.path.abspath(
                os.path.join(provision_dir, "workspace")
            ):
                continue
            shutil.move(src, dst)

    admin_startup_dir = os.path.join(prod_00_dir, "admin_startup")
    if os.path.exists(admin_startup_dir):
        nested_startup = os.path.join(admin_startup_dir, "startup")
        if not os.path.exists(nested_startup):
            os.makedirs(nested_startup, exist_ok=True)
            for file_name in os.listdir(admin_startup_dir):
                if file_name == "startup":
                    continue
                src_path = os.path.join(admin_startup_dir, file_name)
                if os.path.isfile(src_path):
                    shutil.move(
                        src_path, os.path.join(nested_startup, file_name)
                    )

        for folder in ["local", "transfer", "logs"]:
            os.makedirs(os.path.join(admin_startup_dir, folder), exist_ok=True)

        admin_host_file = os.path.join(nested_startup, "server_host.txt")
        if not os.path.exists(admin_host_file):
            sibling_host_file = os.path.join(
                prod_00_dir, "startup", "server_host.txt"
            )
            if os.path.exists(sibling_host_file):
                shutil.copyfile(sibling_host_file, admin_host_file)

        swarm_network.admin_startup_dir = os.path.abspath(admin_startup_dir)
        swarm_network.save(update_fields=["admin_startup_dir"])

    resolved_server_host = ""
    try:
        env_host = os.getenv("SWARMMEDHUB_SERVER_HOST", "").strip()
        if env_host:
            resolved_server_host = env_host

        root_host_file = os.path.join(prod_00_dir, "startup", "server_host.txt")
        if not resolved_server_host and os.path.exists(root_host_file):
            with open(root_host_file) as handle:
                resolved_server_host = handle.read().strip()

        if not resolved_server_host:
            project_yml_path = os.path.join(provision_dir, "project.yml")
            if os.path.exists(project_yml_path):
                with open(project_yml_path) as handle:
                    project_yml = yaml.safe_load(handle) or {}
                for participant in project_yml.get("participants", []):
                    participant_name = str(participant.get("name", "")).strip().lower()
                    listening_host = str(
                        participant.get("listening_host", "")
                    ).strip()
                    if participant_name.startswith("server") and listening_host:
                        if listening_host.lower() not in {
                            "dynamic",
                            "localhost",
                            "127.0.0.1",
                            "server",
                        }:
                            resolved_server_host = listening_host
                            break

        if not resolved_server_host:
            fed_client_json = os.path.join(prod_00_dir, "startup", "fed_client.json")
            if os.path.exists(fed_client_json):
                with open(fed_client_json) as handle:
                    cfg = json.load(handle) or {}
                ha_agent_cfg = cfg.get("overseer_agent", {})
                ha_agent_args = ha_agent_cfg.get("args", {})
                endpoint = (
                    ha_agent_args.get("sp_end_point", "")
                    or ha_agent_args.get("overseer_end_point", "")
                )
                endpoint = str(endpoint).strip()
                if endpoint:
                    if "://" in endpoint:
                        from urllib.parse import urlparse

                        resolved_server_host = (
                            urlparse(endpoint).hostname or ""
                        ).strip()
                    else:
                        resolved_server_host = endpoint.split(":")[0].strip()

                    if resolved_server_host.lower() in {
                        "",
                        "dynamic",
                        "localhost",
                        "127.0.0.1",
                        "server",
                    }:
                        resolved_server_host = ""

        if resolved_server_host:
            for item in os.scandir(prod_00_dir):
                if not item.is_dir():
                    continue
                startup_dir = os.path.join(item.path, "startup")
                if os.path.isdir(startup_dir):
                    with open(
                        os.path.join(startup_dir, "server_host.txt"), "w"
                    ) as handle:
                        handle.write(resolved_server_host)

            root_startup = os.path.join(prod_00_dir, "startup")
            if os.path.isdir(root_startup):
                with open(
                    os.path.join(root_startup, "server_host.txt"), "w"
                ) as handle:
                    handle.write(resolved_server_host)

            log.network.info(
                f"Resolved imported startup kit server host: {resolved_server_host}"
            )
    except Exception as exc:
        log.network.warning(
            f"Could not derive server host from imported startup kit: {exc}"
        )

    swarm_network.status = "PROVISIONED"

    discovered_participants: list[dict] = []
    _reset_package_source(package_source)
    try:
        with zipfile.ZipFile(package_source, "r") as zip_ref:
            file_list = zip_ref.namelist()
            if ".gossip_token" in file_list:
                recovered_token = (
                    read_entry(zip_ref, ".gossip_token").decode("utf-8").strip()
                )
                if recovered_token:
                    swarm_network.gossip_token = recovered_token
                    log.network.info(
                        "Recovered shared gossip token from import."
                    )

            if ".participants.json" in file_list:
                try:
                    participant_data = json.loads(
                        read_entry(zip_ref, ".participants.json").decode("utf-8")
                    )
                    if isinstance(participant_data, list):
                        for participant in participant_data:
                            participant_ip = participant.get("ip", "-")
                            if str(participant_ip).lower() in [
                                "dynamic",
                                "localhost",
                                "127.0.0.1",
                                "server",
                            ]:
                                if (
                                    participant.get("role") == "SERVER"
                                    and resolved_server_host
                                ):
                                    participant_ip = resolved_server_host
                                else:
                                    participant_ip = "-"
                            discovered_participants.append(
                                {
                                    "name": participant.get("participant_id"),
                                    "role": participant.get("role"),
                                    "ip": participant_ip,
                                    "org": participant.get("org"),
                                }
                            )
                        log.network.info(
                            f"Recovered {len(discovered_participants)} participants from .participants.json"
                        )
                except Exception as json_err:
                    log.network.warning(
                        f"Failed to parse .participants.json: {json_err}"
                    )
    except Exception as zip_err:
        log.network.warning(
            f"Failed to read metadata from zip: {zip_err}"
        )

    swarm_network.save()

    try:
        if not discovered_participants:
            project_yml_path = os.path.join(provision_dir, "project.yml")
            if os.path.exists(project_yml_path):
                with open(project_yml_path) as handle:
                    project_yml = yaml.safe_load(handle) or {}
                for participant in project_yml.get("participants", []):
                    participant_name = str(
                        participant.get("name", "")
                    ).strip()
                    participant_type = str(
                        participant.get("type", participant.get("role", ""))
                    ).lower()
                    participant_ip = str(
                        participant.get("client_ip")
                        or participant.get("listening_host", "")
                    ).strip()
                    if not participant_name:
                        continue
                    role = "SERVER" if participant_type == "server" else "CLIENT"
                    if participant_ip.lower() in [
                        "dynamic",
                        "localhost",
                        "127.0.0.1",
                        "server",
                    ]:
                        if role == "SERVER" and resolved_server_host:
                            participant_ip = resolved_server_host
                        else:
                            participant_ip = "-"
                    discovered_participants.append(
                        {
                            "name": participant_name,
                            "role": role,
                            "ip": participant_ip,
                            "org": None,
                        }
                    )

            client_cfgs = [
                os.path.join(prod_00_dir, "startup", "fed_client.json"),
                os.path.join(provision_dir, "startup", "fed_client.json"),
            ]
            for cfg_path in client_cfgs:
                if os.path.exists(cfg_path):
                    try:
                        with open(cfg_path) as handle:
                            data = json.load(handle)
                        client_name = data.get("client_name") or data.get("name")
                        if client_name and client_name != "server":
                            if not any(
                                item["name"] == client_name
                                for item in discovered_participants
                            ):
                                discovered_participants.append(
                                    {
                                        "name": client_name,
                                        "role": "CLIENT",
                                        "ip": "-",
                                        "org": None,
                                    }
                                )
                    except Exception:
                        pass

        swarm_network.participants.all().delete()
        for participant in discovered_participants:
            SwarmParticipant.objects.create(
                network=swarm_network,
                user=actor,
                role=participant["role"],
                participant_id=participant["name"],
                ip=participant.get("ip", "-"),
                org=participant.get("org")
                or f"org_{participant['name'].replace('-', '_')}",
            )

        if discovered_participants:
            log.network.info(
                f"Registered {len(discovered_participants)} participant(s) with IP metadata."
            )
    except Exception as participant_error:
        log.network.warning(
            f"Could not populate participants from imported kit: {participant_error}"
        )

    log.network.info(
        f"Startup kit extracted and network '{name}' marked as PROVISIONED."
    )
    return swarm_network


def export_startup_package(network: SwarmNetwork) -> io.BytesIO:
    """Build the startup package ZIP for a network, and record its fingerprint.

    The network fingerprint (the SHA-256 of the package's ``manifest.json``) and
    the SHA-256 of the ZIP are stored on the network, so that this Hub shows the
    values the sites compare.

    Raises:
        LookupError: If this Hub holds no site's startup kit for the network, as at
            a Hub that imported a single site's kit; nothing is stored then.
        ValueError: If the kits disagree with each other or with the network's
            SERVER participant, for example about the FL server's address, as
            after an import with SWARMMEDHUB_SERVER_HOST set to another address.
    """
    try:
        zip_buffer = create_startup_kits_zip(network)
    except PackageVerificationError as exc:
        # At the coordinating site, SWARMMEDHUB_SERVER_HOST never rewrites kits.
        hint = (
            " An import with SWARMMEDHUB_SERVER_HOST set writes that address into "
            "the kits; export the package at the coordinating site."
            if network.creation_method == "IMPORTED"
            else ""
        )
        raise ValueError(
            f"The startup package of network '{network.name}' was not exported: "
            f"{exc}.{hint}"
        ) from exc
    package_bytes = zip_buffer.getvalue()
    if len(package_bytes) <= 22:
        raise LookupError(
            f"No site's startup kit of network '{network.name}' is at this Hub, so "
            "there is no startup package to export. The network may not have been "
            "provisioned correctly, or this Hub imported a single site's kit, from "
            "which the package cannot be exported; export it at the coordinating "
            "site."
        )
    network.package_fingerprint = package_fingerprint(package_bytes)
    network.package_sha256 = sha256_hex(package_bytes)
    network.save(update_fields=["package_fingerprint", "package_sha256"])
    return zip_buffer


def root_ca_fingerprint(network: SwarmNetwork) -> str:
    """Return the SHA-256 fingerprint of the network's root CA certificate here, or ''.

    The format is OpenSSL's, as ``openssl x509 -noout -fingerprint -sha256`` prints
    it for ``rootCA.pem`` in any of the network's kits.
    """
    prod_dir = network_prod_dir(network)
    candidates = [
        prod_dir / "server" / "startup" / "rootCA.pem",
        *sorted(prod_dir.glob("*/startup/rootCA.pem")),
        # A single site's kit imported on its own.
        prod_dir / "startup" / "rootCA.pem",
    ]
    for path in candidates:
        try:
            return certificate_fingerprint(path.read_bytes())
        except (OSError, ValueError):
            continue
    return ""


def start_network(network: SwarmNetwork, actor: User) -> SwarmNetwork:
    """Enqueue network startup."""
    log = get_logger(user=actor, project=network.project)
    log.network.info(
        f"Starting swarm network '{network.name}' (ID: {network.identifier})."
    )
    network.status = "STARTING"
    network.save(update_fields=["status"])
    run_inline = os.getenv("SWARMMEDHUB_RUN_TASKS_INLINE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if run_inline:
        start_swarm_network_task(str(network.identifier), actor.id)
    else:
        start_swarm_network_task.delay(str(network.identifier), actor.id)
    return network


def failover_network(
    network: SwarmNetwork,
    actor: User,
    *,
    server_host: str,
    start_server: bool = False,
) -> SwarmNetwork:
    """Enqueue a switch of this Hub's sites to another FL server.

    Args:
        network: The network whose server was lost.
        actor: The user making the switch.
        server_host: Address of the server to use from now on.
        start_server: Start the FL server at this site (the standby) first.
    """
    server_host = str(server_host or "").strip()
    if not is_valid_server_host(server_host):
        raise ValueError(
            "--server-host must be an IPv4 address without leading zeros, such as "
            f"192.0.2.10, got {server_host!r}."
        )
    log = get_logger(user=actor, project=network.project)
    log.network.info(
        f"Switching network '{network.name}' (ID: {network.identifier}) to the FL "
        f"server at {server_host}"
        + (", started at this site." if start_server else ".")
    )
    previous_status = network.status
    network.status = "STARTING"
    network.save(update_fields=["status"])
    run_inline = os.getenv("SWARMMEDHUB_RUN_TASKS_INLINE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    task_args = (
        str(network.identifier),
        actor.id,
        server_host,
        bool(start_server),
        previous_status,
    )
    if run_inline:
        failover_network_task(*task_args)
    else:
        failover_network_task.delay(*task_args)
    return network


def describe_server_switch(network: SwarmNetwork) -> dict:
    """Return which FL server this Hub uses for a network.

    ``admin_session_hosts`` lists the addresses that the Hub's admin session,
    which submits and monitors jobs, tries in this order. After a switch that is
    the recorded server alone, except at the Hub that started that server.
    """
    from training.runtime import admin_session_hosts

    record = active_server_record(network)
    provisioned = provisioned_server_host(network)
    return {
        "server_host": record.get("server_host") or unswitched_server_host(network),
        "provisioned_server_host": provisioned,
        "switched": bool(record.get("server_host")),
        "switch": record or None,
        "admin_session_hosts": admin_session_hosts(network),
    }


def stop_network(network: SwarmNetwork, actor: User) -> SwarmNetwork:
    """Enqueue network shutdown."""
    log = get_logger(user=actor, project=network.project)
    log.network.info(
        f"Stopping swarm network '{network.name}' (ID: {network.identifier})."
    )
    network.status = "STOPPING"
    network.save(update_fields=["status"])
    stop_swarm_network_task.delay(str(network.identifier), actor.id)
    return network


def serialize_network(
    network: SwarmNetwork,
    *,
    current_for: User | None = None,
    include_participants: bool = False,
) -> dict:
    """Serialize a network for CLI output."""
    current_network = get_current_network(current_for, network.project) if current_for else None
    participant_rows: list[dict] = []
    if include_participants:
        participant_rows = [
            {
                "identifier": participant.participant_id,
                "role": participant.role,
                "org": participant.org,
                "ip": participant.ip,
                "status": participant.status,
                "last_seen": participant.last_seen.isoformat()
                if participant.last_seen
                else None,
            }
            for participant in network.participants.all().order_by(
                "role", "participant_id"
            )
        ]

    return {
        "identifier": str(network.identifier),
        "name": network.name,
        "description": network.description,
        "status": network.status,
        "creation_method": network.creation_method,
        "project_identifier": str(network.project.identifier),
        "is_current": bool(current_network and current_network.id == network.id),
        # The startup package this Hub last exported or imported.
        "package_fingerprint": network.package_fingerprint,
        "package_sha256": network.package_sha256,
        "root_ca_sha256": root_ca_fingerprint(network),
        "participants": participant_rows,
        "created_at": network.created_at.isoformat(),
        "updated_at": network.updated_at.isoformat(),
    }
