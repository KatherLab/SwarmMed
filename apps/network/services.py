"""Shared network services used by the UI and CLI."""

from __future__ import annotations

import io
import json
import os
import shutil
import zipfile
from pathlib import Path

import yaml
from django.conf import settings
from django.contrib.auth.models import User
from django.utils.text import slugify

from logs.logger import get_logger
from project.models import Project

from .models import SwarmNetwork, SwarmParticipant, UserCurrentNetwork
from .provision import generate_flare_startup_kit, is_valid_ip
from .tasks import start_swarm_network_task, stop_swarm_network_task
from .utils import create_startup_kits_zip, get_hostname, get_tailscale_ip


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
) -> SwarmNetwork:
    """Create and provision a real network startup package."""
    participants = participants or []
    log = get_logger(user=actor, project=project)

    swarm_network = SwarmNetwork.objects.create(
        name=name,
        project=project,
        description=description,
        author=actor,
        creation_method="CREATED",
    )
    log.network.info(
        f"Initialized new network record: {name} (ID: {swarm_network.identifier})"
    )

    resolved_server_ip = (
        server_ip if server_ip and is_valid_ip(server_ip) else get_tailscale_ip()
    )
    clients = []
    for participant in participants:
        safe_name = slugify(participant.get("name", "client"))
        if not safe_name:
            continue
        clients.append({"name": safe_name, "ip": participant.get("ip", "")})

    local_client_name = slugify(get_hostname() or "")
    local_client_ip = (
        resolved_server_ip
        if is_valid_ip(resolved_server_ip)
        else "host.docker.internal"
    )
    if local_client_name:
        clients.append({"name": local_client_name, "ip": local_client_ip})

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
    return swarm_network


def create_local_test_network(
    *, project: Project, actor: User, name: str, description: str = ""
) -> SwarmNetwork:
    """Create and provision a local-test network."""
    log = get_logger(user=actor, project=project)
    swarm_network = SwarmNetwork.objects.create(
        name=name,
        project=project,
        description=description,
        author=actor,
        creation_method="LOCAL_TEST",
    )
    log.network.info(f"Provisioning local testing network '{name}'.")
    generate_flare_startup_kit(
        network_id=swarm_network.identifier,
        local_test=True,
        clients=[],
    )
    return swarm_network


def _reset_package_source(package_source) -> None:
    if hasattr(package_source, "seek"):
        package_source.seek(0)


def _read_zip_namelist(package_source) -> tuple[str | None, list[str]]:
    original_network_id = None
    with zipfile.ZipFile(package_source, "r") as zip_peek:
        file_list = zip_peek.namelist()
        if ".network_id" in file_list:
            original_network_id = (
                zip_peek.read(".network_id").decode("utf-8").strip()
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
) -> SwarmNetwork:
    """Import a network from a startup package zip."""
    log = get_logger(user=actor, project=project)
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
        "creation_method": "UPLOADED",
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

    swarm_network = SwarmNetwork.objects.create(**create_args)
    log.network.info(
        f"Initialized uploaded network record: {name} (ID: {swarm_network.identifier})"
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
                    handle.write(zip_ref.read(member))
            else:
                zip_ref.extract(member, provision_dir)

    project_name = slugify(project.title).replace("-", "_")
    prod_00_dir = os.path.join(
        provision_dir, "workspace", project_name, "prod_00"
    )

    if os.path.exists(os.path.join(provision_dir, "startup")):
        os.makedirs(prod_00_dir, exist_ok=True)
        for item in os.listdir(provision_dir):
            if item in {"workspaces", "runtime_requirements.txt"}:
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
        env_host = os.getenv("MEDSWARMHUB_SERVER_HOST", "").strip()
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
                f"Resolved uploaded startup kit server host: {resolved_server_host}"
            )
    except Exception as exc:
        log.network.warning(
            f"Could not derive server host from uploaded startup kit: {exc}"
        )

    swarm_network.status = "PROVISIONED"

    discovered_participants: list[dict] = []
    _reset_package_source(package_source)
    try:
        with zipfile.ZipFile(package_source, "r") as zip_ref:
            file_list = zip_ref.namelist()
            if ".gossip_token" in file_list:
                recovered_token = (
                    zip_ref.read(".gossip_token").decode("utf-8").strip()
                )
                if recovered_token:
                    swarm_network.gossip_token = recovered_token
                    log.network.info(
                        "Recovered shared gossip token from upload."
                    )

            if ".participants.json" in file_list:
                try:
                    participant_data = json.loads(
                        zip_ref.read(".participants.json").decode("utf-8")
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
                        participant.get("listening_host", "")
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
            f"Could not populate participants from uploaded kit: {participant_error}"
        )

    log.network.info(
        f"Startup kit extracted and network '{name}' marked as PROVISIONED."
    )
    return swarm_network


def export_startup_package(network: SwarmNetwork) -> io.BytesIO:
    """Build the startup package ZIP for a network."""
    zip_buffer = create_startup_kits_zip(network)
    if len(zip_buffer.getvalue()) <= 22:
        raise LookupError(
            "Startup kits were not found. The network may not have been provisioned correctly."
        )
    return zip_buffer


def start_network(network: SwarmNetwork, actor: User) -> SwarmNetwork:
    """Enqueue network startup."""
    log = get_logger(user=actor, project=network.project)
    log.network.info(
        f"Starting swarm network '{network.name}' (ID: {network.identifier})."
    )
    network.status = "STARTING"
    network.save(update_fields=["status"])
    start_swarm_network_task.delay(str(network.identifier), actor.id)
    return network


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
        "participants": participant_rows,
        "created_at": network.created_at.isoformat(),
        "updated_at": network.updated_at.isoformat(),
    }
