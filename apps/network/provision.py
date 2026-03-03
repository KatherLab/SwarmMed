"""
NVFlare Provisioning Logic.
Handles the generation of project.yml and execution of 'nvflare provision'
to create secure startup kits for federated learning participants.
"""

import json
import os
import re
import shutil
import subprocess  # nosec B404
from pathlib import Path

import yaml
from common.utils import get_s3_client
from django.conf import settings
from django.utils.text import slugify
from logs.logger import get_logger

from .models import SwarmNetwork
from .utils import get_hostname


def is_valid_ip(ip):
    """
    Basic validation for IPv4 addresses or 'host.docker.internal'.
    """
    if ip == "host.docker.internal":
        return True
    # Simple regex for IPv4
    pattern = r"^(\d{1,3}\.){3}\d{1,3}$"
    return bool(re.match(pattern, ip))


def generate_flare_startup_kit(
    network_id,
    local_test=False,
    clients=None,
    server_ip=None,
    ha_servers=None,
):
    """
    Generates the startup kits for a given SwarmNetwork using NVFlare.

    This function:
    1. Creates a workspace directory for the specific network.
    2. Builds a project.yml file describing the network topology.
    3. Fetches optional project requirements from S3 storage.
    4. Runs the NVFlare Lighter provisioning tool.
    5. Updates configuration files with the provided server IP if applicable.
    """
    if clients is None:
        clients = []

    try:
        # Retrieve the network object and setup logging context
        network = SwarmNetwork.objects.get(identifier=network_id)
        logger = get_logger(project=network.project)
    except SwarmNetwork.DoesNotExist:
        # If the network doesn't exist, we can't proceed
        return

    # Define paths for the specific project and network
    workspaces_root = os.path.join(settings.BASE_DIR, "workspaces")
    project_dir = os.path.abspath(
        os.path.join(workspaces_root, str(network.project.identifier))
    )
    provision_dir = os.path.abspath(
        os.path.join(project_dir, str(network.identifier))
    )

    # Security check: Ensure provision_dir is strictly within workspaces_root
    # This prevents any potential path traversal if identifiers are malicious
    if not provision_dir.startswith(os.path.join(workspaces_root, "")):
        logger.network.error(
            f"Security Error: Invalid provision directory: {provision_dir}"
        )
        return

    # Resolve and read the template BEFORE cleanup in case the source path is
    # located inside provision_dir for this runtime environment.
    repo_template = Path(__file__).resolve().parent / "master_template.yml"
    if not repo_template.exists():
        logger.network.error(
            f"ERROR: master_template.yml missing at {repo_template}"
        )
        return

    try:
        template_payload = repo_template.read_text()
    except Exception as e:
        logger.network.error(f"Failed to read master template: {e}")
        return

    # Clean start: remove any existing provisioning directory for this ID
    if os.path.exists(provision_dir):
        shutil.rmtree(provision_dir)
    os.makedirs(provision_dir, exist_ok=True)

    # 1. Setup Template and Filesystem
    target_template = Path(provision_dir) / "master_template.yml"
    target_template.write_text(template_payload)

    # If a server IP is provided, inject it into the template before provisioning
    # so that generated config files remain signed/secure.
    if server_ip and is_valid_ip(server_ip):
        try:
            template_text = target_template.read_text()
            updated = template_text.replace(
                "https://overseer:8443/api/v1",
                f"https://{server_ip}:8443/api/v1",
            )
            updated = updated.replace(
                "https://overseer:8443", f"https://{server_ip}:8443"
            )
            updated = updated.replace("${SERVER_IP}", server_ip)
            if template_text != updated:
                target_template.write_text(updated)
                logger.network.info(
                    f"Injected overseer endpoint and server IP {server_ip} into master template"
                )
        except Exception as e:
            logger.network.warning(
                f"Failed to inject overseer IP into template: {e}"
            )

    template_file_name = target_template.name

    # 2. Define Network Participants
    # Every network needs an overseer and an admin account
    control_plane_org = "swarm_control_plane"
    overseer_participant = {
        "name": "overseer",
        "type": "overseer",
        "org": control_plane_org,
        "protocol": "https",
        "api_root": "/api/v1",
        "port": 8443,
    }
    if server_ip and is_valid_ip(server_ip):
        # Ensure generated endpoints use the server's reachable IP.
        # NVFlare uses different keys depending on participant type.
        overseer_participant["host"] = server_ip
        overseer_participant["listening_host"] = server_ip

    participants = [overseer_participant]

    valid_clients = []
    client_admin_map = {}
    client_server_map = {}

    if local_test:
        # Local test mode: add generic clients for testing on a single machine
        participants.append(
            {
                "name": "server",
                "type": "server",
                "org": control_plane_org,
                "fed_learn_port": 8002,
                "admin_port": 8003,
            }
        )
        participants.extend(
            [
                {"name": "fl-client-1", "type": "client", "org": "nvidia"},
                {"name": "fl-client-2", "type": "client", "org": "nvidia"},
            ]
        )
    else:
        # Real deployment:
        # 1) sanitize client list
        # 2) add HA servers in one control-plane org
        # 3) add clients/admins and map each center to one server
        prepared_clients = []
        for client in clients:
            safe_client_name = slugify(client["name"])
            ip = client.get("ip", "")
            if not is_valid_ip(ip):
                logger.network.warning(
                    f"Skipping client {safe_client_name} due to invalid IP: {ip}"
                )
                continue

            center_org_name = f"org_{safe_client_name.replace('-', '_')}"
            prepared_clients.append(
                {
                    "name": safe_client_name,
                    "ip": ip,
                    "org": center_org_name,
                }
            )

        if ha_servers is not None:
            try:
                server_count = max(1, int(ha_servers))
            except (TypeError, ValueError):
                server_count = max(1, len(prepared_clients))
        else:
            server_count = max(1, len(prepared_clients))

        server_names = []
        for server_index in range(server_count):
            server_name = f"server{server_index + 1}"
            fed_learn_port = 8002 + (100 * server_index)
            admin_port = fed_learn_port + 1

            participants.append(
                {
                    "name": server_name,
                    "type": "server",
                    "org": control_plane_org,
                    "fed_learn_port": fed_learn_port,
                    "admin_port": admin_port,
                }
            )
            server_names.append(server_name)

        for client_index, client_info in enumerate(prepared_clients):
            safe_client_name = client_info["name"]
            ip = client_info["ip"]
            center_org_name = client_info["org"]

            mapped_server = server_names[client_index % len(server_names)]

            participants.append(
                {
                    "name": safe_client_name,
                    "type": "client",
                    "org": center_org_name,
                    "listening_host": ip,
                }
            )
            valid_clients.append({"name": safe_client_name, "ip": ip})
            client_server_map[safe_client_name] = mapped_server

            admin_name = f"admin-{safe_client_name}@nvidia.com"
            participants.append(
                {
                    "name": admin_name,
                    "type": "admin",
                    "org": center_org_name,
                    "role": "project_admin",
                }
            )
            client_admin_map[safe_client_name] = admin_name

    ha_overseer_client_name = None
    if not local_test and valid_clients:
        if server_ip and is_valid_ip(server_ip):
            matching = [c["name"] for c in valid_clients if c["ip"] == server_ip]
            if matching:
                ha_overseer_client_name = matching[0]

        if not ha_overseer_client_name:
            try:
                creator_center_name = slugify(get_hostname())
                if creator_center_name:
                    name_matches = [
                        c["name"]
                        for c in valid_clients
                        if c["name"] == creator_center_name
                    ]
                    if name_matches:
                        ha_overseer_client_name = name_matches[0]
            except Exception:
                ha_overseer_client_name = None

        if not ha_overseer_client_name:
            ha_overseer_client_name = valid_clients[0]["name"]
            logger.network.info(
                f"No client matched server IP for overseer role; defaulting overseer startup assignment to {ha_overseer_client_name}"
            )

    if local_test:
        # Keep a single admin identity for local development mode.
        participants.append(
            {
                "name": "admin@nvidia.com",
                "type": "admin",
                "org": "nvidia",
                "role": "project_admin",
            }
        )

    # 3. Generate project.yml content using safe_dump to prevent injection
    project_name_safe = slugify(network.project.title).replace("-", "_")
    project_config = {
        "api_version": 3,
        "name": project_name_safe,
        "description": f"FLARE project for {network.project.title}",
        "participants": participants,
        "builders": [
            {
                "path": "nvflare.lighter.impl.workspace.WorkspaceBuilder",
                "args": {"template_file": template_file_name},
            },
            {
                "path": "nvflare.lighter.impl.docker.DockerBuilder",
                "args": {
                    "base_image": "python:3.10-slim",
                    "requirements_file": "docker_compose_requirements.txt",
                },
            },
            {
                "path": "nvflare.lighter.impl.static_file.StaticFileBuilder",
                "args": {
                    "overseer_agent": {
                        "path": "nvflare.ha.overseer_agent.HttpOverseerAgent",
                        "overseer_exists": True,
                    }
                },
            },
            {"path": "nvflare.lighter.impl.cert.CertBuilder"},
            {"path": "nvflare.lighter.impl.signature.SignatureBuilder"},
        ],
    }

    project_yml_path = os.path.join(provision_dir, "project.yml")
    with open(project_yml_path, "w") as f:
        yaml.safe_dump(project_config, f, default_flow_style=False)

    # 4. Handle Python Requirements
    # We create a requirements file that DockerBuilder will inject into images
    req_file_path = os.path.join(
        provision_dir, "docker_compose_requirements.txt"
    )
    with open(req_file_path, "w") as rf:
        # Basic requirements for all participants
        rf.write("nvflare==2.4.1\n")
        rf.write("gunicorn\n")
        rf.write("boto3\n")
        rf.write("python-dotenv\n")

        # If the project has a custom requirements file in S3, download and append it
        if network.project.requirements_file:
            try:
                s3_client = get_s3_client()
                bucket = settings.AWS_STORAGE_BUCKET_NAME
                key = network.project.requirements_file.name

                logger.network.info(
                    f"Downloading custom requirements from {key}"
                )
                response = s3_client.get_object(Bucket=bucket, Key=key)
                custom_reqs = response["Body"].read().decode("utf-8")

                # Basic Sanitization: Only allow alphanumeric, underscores, hyphens, and version specifiers
                safe_lines = []
                for line in custom_reqs.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Match basic package name and version: e.g. pandas==1.2.3, torch>=2.0
                    if re.match(
                        r"^[a-zA-Z0-9_\-\[\]]+([=<>!~]+[a-zA-Z0-9\._\-\*\,]+)?$",
                        line,
                    ):
                        safe_lines.append(line)
                    else:
                        logger.network.warning(
                            f"Skipping potentially unsafe requirement line: {line}"
                        )

                if safe_lines:
                    rf.write("\n# Project specific requirements (sanitized)\n")
                    rf.write("\n".join(safe_lines) + "\n")
            except Exception as e:
                logger.network.warning(
                    f"Could not fetch custom requirements: {e}"
                )

    # 5. Run NVFlare Provisioning
    try:
        logger.network.info(f"Running nvflare provision in {provision_dir}")
        nvflare_path = shutil.which("nvflare") or "nvflare"
        command = [
            nvflare_path,
            "provision",
            "-p",
            "project.yml",
            "-w",
            "workspace",
        ]

        # Execute the lighter tool to generate certificates and startup kits
        result = subprocess.run(  # nosec B603
            command,
            cwd=provision_dir,
            capture_output=True,
            text=True,
            check=True,
        )

        # Verify the output directory was created
        base_prod_path = (
            Path(provision_dir) / "workspace" / project_name_safe / "prod_00"
        )

        if not base_prod_path.exists():
            logger.network.error(
                f"Provisioning completed with exit code 0 but 'prod_00' is missing.\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
            network.status = "ERROR"
            network.save()
            return

        logger.network.info("Provisioning completed successfully")

        if not local_test:
            try:
                (base_prod_path / ".ha_distribution_enabled").write_text("1")
                if ha_overseer_client_name:
                    (base_prod_path / ".overseer_client").write_text(
                        ha_overseer_client_name
                    )
                    logger.network.info(
                        f"HA startup distribution enabled: overseer startup will be bundled for client {ha_overseer_client_name}"
                    )
                else:
                    logger.network.info(
                        "HA startup distribution enabled: no overseer target client selected"
                    )

                if client_admin_map:
                    (base_prod_path / ".client_admin_map.json").write_text(
                        json.dumps(client_admin_map)
                    )

                if client_server_map:
                    (base_prod_path / ".client_server_map.json").write_text(
                        json.dumps(client_server_map)
                    )
            except Exception as e:
                logger.network.warning(
                    f"Failed to persist HA startup distribution metadata: {e}"
                )

        if server_ip and is_valid_ip(server_ip):
            # Post-process only if compose exists (compose is not signed by NVFlare).
            base_prod_path = (
                Path(provision_dir) / "workspace" / project_name_safe / "prod_00"
            )
            compose_path = base_prod_path / "compose.yaml"

            if compose_path.exists():
                try:
                    with open(compose_path, "r") as f:
                        content = f.read()

                    # Replace placeholder tokens if present; if already resolved, this is a no-op.
                    new_content = content.replace("${SERVER_IP}", server_ip)

                    # Parse YAML to ensure extra_hosts are present for clients (belt-and-suspenders).
                    compose_data = yaml.safe_load(new_content) or {}
                    services = compose_data.get("services", {})

                    server_aliases = set(client_server_map.values())

                    for svc_name, svc_conf in services.items():
                        name_lower = str(svc_name).lower()
                        if name_lower in {"__flclient__", "fl_client", "client", "flclient"}:
                            extra_hosts = svc_conf.get("extra_hosts", []) or []
                            host_entries = {
                                f"overseer:{server_ip}",
                                f"server:{server_ip}",
                            }
                            host_entries.update(
                                {
                                    f"{alias}:{server_ip}"
                                    for alias in server_aliases
                                    if alias
                                }
                            )
                            existing_set = set(extra_hosts)
                            merged_hosts = list(existing_set.union(host_entries))
                            svc_conf["extra_hosts"] = merged_hosts

                    compose_data["services"] = services

                    with open(compose_path, "w") as f:
                        yaml.safe_dump(compose_data, f, default_flow_style=False)
                    logger.network.info(
                        f"Ensured compose.yaml has extra_hosts for overseer/server at {server_ip} (compose is unsigned)"
                    )
                except Exception as e:
                    logger.network.warning(f"Failed to update compose.yaml: {e}")

            # Write overseer host into client kits to help upload-side compose generation.
            try:
                server_aliases = list(client_server_map.values())
                for item in os.scandir(str(base_prod_path)):
                    if not item.is_dir():
                        continue
                    if (
                        item.name in {"server", "overseer"}
                        or item.name.startswith("server")
                        or "admin" in item.name
                    ):
                        continue
                    startup_dir = Path(item.path) / "startup"
                    if startup_dir.exists():
                        (startup_dir / "overseer_host.txt").write_text(server_ip)
                        if server_aliases:
                            (startup_dir / "server_aliases.txt").write_text(
                                "\n".join(server_aliases) + "\n"
                            )
            except Exception as e:
                logger.network.warning(
                    f"Failed to write overseer_host.txt into client kits: {e}"
                )

        # Update network status in the database
        network.status = "PROVISIONED"
        network.save()

    except subprocess.CalledProcessError as e:
        logger.network.error(
            f"NVFlare provision failed (Exit {e.returncode}):\nSTDOUT: {e.stdout}\nSTDERR: {e.stderr}"
        )
        network.status = "ERROR"
        network.save()
    except Exception as e:
        logger.network.error(f"Unexpected error during provisioning: {e}")
        network.status = "ERROR"
        network.save()
