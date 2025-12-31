"""
NVFlare Provisioning Logic.
Handles the generation of project.yml and execution of 'nvflare provision'
to create secure startup kits for federated learning participants.
"""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

from django.conf import settings

from apps.data.utils import get_s3_client
from apps.logs.logger import get_logger
from .models import SwarmNetwork


def generate_flare_startup_kit(network_id, local_test=False, clients=None):
    """
    Generates the startup kits for a given SwarmNetwork using NVFlare.

    This function:
    1. Creates a workspace directory for the specific network.
    2. Builds a project.yml file describing the network topology.
    3. Fetches optional project requirements from S3 storage.
    4. Runs the NVFlare Lighter provisioning tool.
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
    project_dir = os.path.join('workspaces', str(network.project.identifier))
    provision_dir = os.path.join(project_dir, str(network.identifier))

    # Clean start: remove any existing provisioning directory for this ID
    if os.path.exists(provision_dir):
        shutil.rmtree(provision_dir)
    os.makedirs(provision_dir)

    # 1. Setup Template and Filesystem
    # Copy the master_template.yml from the app directory to the workspace
    repo_template = Path(__file__).resolve().parent / 'master_template.yml'
    target_template = Path(provision_dir) / 'master_template.yml'

    if not repo_template.exists():
        logger.network.error(
            f"ERROR: master_template.yml missing at {repo_template}")
        return

    shutil.copyfile(str(repo_template), str(target_template))
    abs_template_path = str(target_template.resolve())

    # 2. Define Network Participants
    # Every network needs an overseer and an admin account
    participants = [
        {
            'name': 'overseer',
            'type': 'overseer',
            'org': 'nvidia',
            'protocol': 'https',
            'api_root': '/api/v1',
            'port': 8443,
        }
    ]

    # Add the central FL server
    participants.append({
        'name': 'server',
        'type': 'server',
        'org': 'nvidia',
        'fed_learn_port': 8002,
        'admin_port': 8003,
    })

    if local_test:
        # Local test mode: add generic clients for testing on a single machine
        participants.extend([
            {'name': 'fl-client-1', 'type': 'client', 'org': 'nvidia'},
            {'name': 'fl-client-2', 'type': 'client', 'org': 'nvidia'},
        ])
    else:
        # Real deployment: add specific clients provided by the user (with IPs)
        for client in clients:
            participants.append({
                'name': client['name'],
                'type': 'client',
                'org': 'nvidia',
                'listening_host': client['ip'],
            })

    # Add the project administrator account
    participants.append({
        'name': 'admin@nvidia.com',
        'type': 'admin',
        'org': 'nvidia',
        'role': 'project_admin',
    })

    # 3. Generate project.yml content
    # We dynamically build the YAML string based on participants defined above
    participants_yaml = ""
    for p in participants:
        participants_yaml += f"      - name: {p['name']}\n"
        participants_yaml += f"        type: {p['type']}\n"
        participants_yaml += f"        org: {p['org']}\n"

        # Optional fields based on participant type
        if 'fed_learn_port' in p:
            participants_yaml += f"        fed_learn_port: {p['fed_learn_port']}\n"
        if 'admin_port' in p:
            participants_yaml += f"        admin_port: {p['admin_port']}\n"
        if 'listening_host' in p:
            participants_yaml += f"        listening_host: {p['listening_host']}\n"
        if 'role' in p:
            participants_yaml += f"        role: {p['role']}\n"
        if 'port' in p:
            participants_yaml += f"        port: {p['port']}\n"
        if 'protocol' in p:
            participants_yaml += f"        protocol: {p['protocol']}\n"
        if 'api_root' in p:
            participants_yaml += f"        api_root: {p['api_root']}\n"

    # Assemble the full project configuration for NVFlare Lighter
    project_name_safe = network.project.title.replace(' ', '_')
    project_yml_content = textwrap.dedent(f"""
    api_version: 3
    name: {project_name_safe}
    description: FLARE project for {network.project.title}

    participants:
{participants_yaml}    builders:
      - path: nvflare.lighter.impl.workspace.WorkspaceBuilder
        args:
          template_file: \"{abs_template_path}\"
      - path: nvflare.lighter.impl.docker.DockerBuilder
        args:
          base_image: python:3.10-slim
          requirements_file: docker_compose_requirements.txt
      - path: nvflare.lighter.impl.static_file.StaticFileBuilder
        args:
            overseer_agent:
                path: nvflare.ha.overseer_agent.HttpOverseerAgent
                overseer_exists: true
      - path: nvflare.lighter.impl.cert.CertBuilder
      - path: nvflare.lighter.impl.signature.SignatureBuilder
    """).strip()

    project_yml_path = os.path.join(provision_dir, 'project.yml')
    with open(project_yml_path, 'w') as f:
        f.write(project_yml_content)

    # 4. Handle Python Requirements
    # We create a requirements file that DockerBuilder will inject into images
    req_file_path = os.path.join(
        provision_dir,
        'docker_compose_requirements.txt')
    with open(req_file_path, 'w') as rf:
        # Basic requirements for all participants
        rf.write('nvflare==2.6.1\n')
        rf.write('gunicorn\n')
        rf.write('boto3\n')
        rf.write('python-dotenv\n')

        # If the project has a custom requirements file in S3, download and append it
        if network.project.requirements_file:
            try:
                s3_client = get_s3_client()
                bucket = settings.AWS_STORAGE_BUCKET_NAME
                key = network.project.requirements_file.name

                logger.network.info(f"Downloading custom requirements from {key}")
                response = s3_client.get_object(Bucket=bucket, Key=key)
                custom_reqs = response['Body'].read().decode('utf-8')
                rf.write('\n# Project specific requirements\n')
                rf.write(custom_reqs)
            except Exception as e:
                logger.network.warning(
                    f"Could not fetch custom requirements: {e}")

    # 5. Run NVFlare Provisioning
    try:
        logger.network.info(f"Running nvflare provision in {provision_dir}")
        command = [
            'nvflare', 'provision',
            '-p', 'project.yml',
            '-w', 'workspace'
        ]

        # Execute the lighter tool to generate certificates and startup kits
        subprocess.run(
            command,
            cwd=provision_dir,
            capture_output=True,
            text=True,
            check=True
        )
        logger.network.info("Provisioning completed successfully")

        # Update network status in the database
        network.status = 'PROVISIONED'
        network.save()

    except subprocess.CalledProcessError as e:
        logger.network.error(f"NVFlare provision failed: {e.stderr}")
        network.status = 'ERROR'
        network.save()
    except Exception as e:
        logger.network.error(f"Unexpected error during provisioning: {e}")
        network.status = 'ERROR'
        network.save()
