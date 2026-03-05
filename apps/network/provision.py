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

    # Clean start: remove any existing provisioning directory for this ID
    if os.path.exists(provision_dir):
        shutil.rmtree(provision_dir)
    os.makedirs(provision_dir, exist_ok=True)

    # 1. Setup Template and Filesystem
    # Copy the master_template.yml from the app directory to the workspace
    repo_template = Path(__file__).resolve().parent / "master_template.yml"
    target_template = Path(provision_dir) / "master_template.yml"

    if not repo_template.exists():
        logger.network.error(
            f"ERROR: master_template.yml missing at {repo_template}"
        )
        return

    shutil.copyfile(str(repo_template), str(target_template))

    # If a server IP is provided, inject it into the template before provisioning
    # so that generated config files remain signed/secure.
    if server_ip and is_valid_ip(server_ip):
        try:
            template_text = target_template.read_text()
            updated = template_text.replace(
                "https://server:8443/api/v1",
                f"https://{server_ip}:8443/api/v1",
            )
            updated = updated.replace(
                "https://server:8443", f"https://{server_ip}:8443"
            )
            updated = updated.replace("${SERVER_IP}", server_ip)
            if template_text != updated:
                target_template.write_text(updated)
                logger.network.info(
                    f"Injected server endpoint and server IP {server_ip} into master template"
                )
        except Exception as e:
            logger.network.warning(
                f"Failed to inject server IP into template: {e}"
            )

    abs_template_path = str(target_template.resolve())

    # 2. Define Network Participants
    control_plane_org = "swarm_control_plane"
    participants = []

    client_admin_map = {}
    client_server_map = {}
    local_client_names = []

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
        # 2) add one server in the control-plane org
        # 3) add clients/admins and map each center to the server
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

        if ha_servers not in (None, 1, "1"):
            logger.network.info(
                "Ignoring ha_servers value because multi-server topology is not supported for NVFlare 2.7.1"
            )

        participants.append(
            {
                "name": "server",
                "type": "server",
                "org": control_plane_org,
                "fed_learn_port": 8002,
                "admin_port": 8003,
            }
        )

        for client_info in prepared_clients:
            safe_client_name = client_info["name"]
            ip = client_info["ip"]
            center_org_name = client_info["org"]

            if server_ip and ip == server_ip:
                local_client_names.append(safe_client_name)

            mapped_server = "server"

            participants.append(
                {
                    "name": safe_client_name,
                    "type": "client",
                    "org": center_org_name,
                    "listening_host": ip,
                }
            )
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
                "args": {"template_file": abs_template_path},
            },
            {
                "path": "nvflare.lighter.impl.docker.DockerBuilder",
                "args": {
                    "base_image": "python:3.12-slim",
                    "requirements_file": "docker_compose_requirements.txt",
                },
            },
            {
                "path": "nvflare.lighter.impl.static_file.StaticFileBuilder",
                "args": {},
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
        rf.write("nvflare==2.7.1\n")
        rf.write("gunicorn\n")
        rf.write("boto3\n")
        rf.write("python-dotenv\n")
        rf.write("pandas\n")
        rf.write("numpy\n")

        # Collect additional requirements from supported sources in S3.
        # Source 1: project.requirements_file (explicit upload in Project settings)
        # Source 2: <project_id>/code/training/requirements.txt (training code bundle)
        s3_client = get_s3_client()
        bucket = settings.AWS_STORAGE_BUCKET_NAME
        requirement_sources = []

        if network.project.requirements_file:
            requirement_sources.append(network.project.requirements_file.name)

        training_requirements_key = (
            f"{network.project.identifier}/code/training/requirements.txt"
        )
        if training_requirements_key not in requirement_sources:
            requirement_sources.append(training_requirements_key)

        safe_lines = []
        seen_requirements = set()
        for key in requirement_sources:
            try:
                logger.network.info(
                    f"Downloading custom requirements from {key}"
                )
                response = s3_client.get_object(Bucket=bucket, Key=key)
                custom_reqs = response["Body"].read().decode("utf-8")

                for line in custom_reqs.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if re.match(
                        r"^[a-zA-Z0-9_\-\[\]]+([=<>!~]+[a-zA-Z0-9\._\-\*\,]+)?$",
                        line,
                    ):
                        normalized = line.lower()
                        if normalized not in seen_requirements:
                            safe_lines.append(line)
                            seen_requirements.add(normalized)
                    else:
                        logger.network.warning(
                            f"Skipping potentially unsafe requirement line: {line}"
                        )
            except Exception as e:
                logger.network.info(
                    f"No readable requirements at {key}: {e}"
                )

        if safe_lines:
            rf.write("\n# Project specific requirements (sanitized)\n")
            rf.write("\n".join(safe_lines) + "\n")

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
                if client_admin_map:
                    (base_prod_path / ".client_admin_map.json").write_text(
                        json.dumps(client_admin_map)
                    )

                if client_server_map:
                    (base_prod_path / ".client_server_map.json").write_text(
                        json.dumps(client_server_map)
                    )

                if local_client_names:
                    (base_prod_path / ".local_client_names.json").write_text(
                        json.dumps(sorted(set(local_client_names)))
                    )
            except Exception as e:
                logger.network.warning(
                    f"Failed to persist HA startup distribution metadata: {e}"
                )

        if server_ip and is_valid_ip(server_ip):
            base_prod_path = (
                Path(provision_dir) / "workspace" / project_name_safe / "prod_00"
            )

            # Write server host metadata into all kits (client, admin, server)
            # for containerized runtime host mapping and Admin API routing.
            try:
                server_aliases = list(client_server_map.values())
                for item in os.scandir(str(base_prod_path)):
                    if not item.is_dir():
                        continue
                    
                    startup_dir = Path(item.path) / "startup"
                    if startup_dir.exists():
                        (startup_dir / "server_host.txt").write_text(server_ip)
                        
                        # Client kits also get server aliases for docker-compose host mapping
                        if item.name != "server" and not item.name.startswith("server") and "admin" not in item.name:
                            if server_aliases:
                                (startup_dir / "server_aliases.txt").write_text(
                                    "\n".join(server_aliases) + "\n"
                                )
            except Exception as e:
                logger.network.warning(
                    f"Failed to write server_host.txt into kits: {e}"
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
