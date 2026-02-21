"""
NVFlare Provisioning Logic.
Handles the generation of project.yml and execution of 'nvflare provision'
to create secure startup kits for federated learning participants.
"""

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


def generate_flare_startup_kit(network_id, local_test=False, clients=None, server_ip=None):
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
    abs_template_path = str(target_template.resolve())

    # 2. Define Network Participants
    # Every network needs an overseer and an admin account
    participants = [
        {
            "name": "overseer",
            "type": "overseer",
            "org": "nvidia",
            "protocol": "https",
            "api_root": "/api/v1",
            "port": 8443,
        }
    ]

    # Add the central FL server
    participants.append(
        {
            "name": "server",
            "type": "server",
            "org": "nvidia",
            "fed_learn_port": 8002,
            "admin_port": 8003,
        }
    )

    if local_test:
        # Local test mode: add generic clients for testing on a single machine
        participants.extend(
            [
                {"name": "fl-client-1", "type": "client", "org": "nvidia"},
                {"name": "fl-client-2", "type": "client", "org": "nvidia"},
            ]
        )
    else:
        # Real deployment: add specific clients provided by the user (with IPs)
        for client in clients:
            # Sanitize client name for safety
            safe_client_name = slugify(client["name"])
            ip = client.get("ip", "")
            if not is_valid_ip(ip):
                logger.network.warning(
                    f"Skipping client {safe_client_name} due to invalid IP: {ip}"
                )
                continue

            participants.append(
                {
                    "name": safe_client_name,
                    "type": "client",
                    "org": "nvidia",
                    "listening_host": ip,
                }
            )

    # Add the project administrator account
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
        rf.write("nvflare==2.6.1\n")
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

        if server_ip and is_valid_ip(server_ip):
            # Post-process the generated compose.yaml to inject the server IP
            # This ensures extra_hosts resolves correctly without client-side env vars
            base_prod_path = (
                Path(provision_dir) / "workspace" / project_name_safe / "prod_00"
            )
            compose_path = base_prod_path / "compose.yaml"

            if compose_path.exists():
                try:
                    with open(compose_path, "r") as f:
                        content = f.read()
                    
                    # Replace the placeholder with the actual IP
                    new_content = content.replace("${SERVER_IP}", server_ip)
                    
                    if content != new_content:
                        with open(compose_path, "w") as f:
                            f.write(new_content)
                        logger.network.info(f"Injected Server IP {server_ip} into compose.yaml")
                except Exception as e:
                    logger.network.warning(f"Failed to update compose.yaml: {e}")

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
