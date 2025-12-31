"""
Celery tasks for the network app.
Handles long-running operations like Docker deployment,
preflight checks, and real-time log streaming.
"""

import os
import subprocess

import docker
from celery import shared_task
from django.conf import settings
from django.contrib.auth.models import User

from apps.logs.logger import get_logger
from apps.logs.models import LogCategory, LogEntry
from apps.project.models import Project
from .models import SwarmNetwork


@shared_task(bind=True)
def execute_and_log_in_container(
    self,
    container_name,
    command,
    network_id,
    project_id,
    user_id
):
    """
    Executes a shell command inside a running Docker container and
    streams its output (STDOUT/STDERR) directly to the database log entries.
    """
    try:
        network = SwarmNetwork.objects.get(identifier=network_id)
        project = Project.objects.get(identifier=project_id)
        logger = get_logger(project=project)

        # Connect to local Docker daemon
        client = docker.from_env()
        container = client.containers.get(container_name)

        logger.network.info(f"Executing in {container_name}: {command}")

        # Execute the command and stream the results line-by-line
        exec_result = container.exec_run(command, stream=True)

        for line in exec_result.output:
            # Create a database log entry for each line produced by the
            # container
            LogEntry.objects.create(
                user_id=user_id,
                project=project,
                swarm_network=network,
                category=LogCategory.NETWORK,
                source=container_name,
                level='INFO',
                message=line.decode('utf-8').strip()
            )

    except Exception as e:
        # Log failure if container or command execution fails
        internal_logger = get_logger()
        internal_logger.network.error(f"Exec failed in {container_name}: {e}")


@shared_task
def start_swarm_network_task(network_id, user_id):
    """
    Deploys a swarm network using docker-compose.
    1. Prepares the compose file with correct paths.
    2. Builds the required Docker images.
    3. Starts the containers in detached mode.
    4. Connects the main app container to the new network.
    """
    try:
        swarm_network = SwarmNetwork.objects.get(identifier=network_id)
        user = User.objects.get(id=user_id)
        logger = get_logger(user=user, project=swarm_network.project)

        project_name = swarm_network.project.title.replace(' ', '_')
        provision_dir = os.path.join(
            settings.BASE_DIR,
            'workspaces',
            str(swarm_network.project.identifier),
            str(swarm_network.identifier)
        )
        compose_dir = os.path.join(
            provision_dir,
            'workspace',
            project_name,
            'prod_00'
        )
        compose_file_path = os.path.join(compose_dir, 'compose.yaml')

        if not os.path.exists(compose_file_path):
            raise FileNotFoundError(
                f"Compose file missing: {compose_file_path}")

        # 1. Modify compose file for host-path mapping (if applicable)
        host_project_path = os.getenv('HOST_PROJECT_PATH')
        if host_project_path:
            with open(compose_file_path, 'r') as f:
                content = f.read()

            rel_dir = os.path.relpath(compose_dir, settings.BASE_DIR)
            host_dir = os.path.join(host_project_path, rel_dir)

            # Replace relative paths with host-absolute paths for volume
            # mounting
            mappings = {
                'build: ./nvflare': f'build: {os.path.join(host_dir, "nvflare")}',
                './fl-client': os.path.join(host_dir, 'fl-client'),
                './server': os.path.join(host_dir, 'server'),
                './overseer': os.path.join(host_dir, 'overseer'),
            }
            for old, new in mappings.items():
                content = content.replace(old, new)

            with open(compose_file_path, 'w') as f:
                f.write(content)
            logger.network.info("Updated compose file with host paths.")

        # 2. Build Docker images
        logger.network.info("Building Docker images for the network...")
        subprocess.run(
            ['docker', 'compose', '-f', 'compose.yaml', 'build'],
            cwd=compose_dir,
            check=True
        )

        # 3. Start containers
        logger.network.info("Starting Docker containers (detached)...")
        subprocess.run(
            ['docker', 'compose', '-f', 'compose.yaml', 'up', '-d'],
            cwd=compose_dir,
            check=True
        )

        # 4. Network connection
        # Connect the 'swarmcloud' app container to the newly created network
        # so it can communicate with the FLARE overseer/server.
        try:
            # Docker Compose creates network named <dir>_default
            net_name = os.path.basename(compose_dir) + "_default"
            subprocess.run(
                ['docker', 'network', 'connect', net_name, 'swarmcloud'],
                capture_output=True
            )
        except Exception as e:
            logger.network.warning(
                f"Could not connect app to flare network: {e}")

        # Mark as running and trigger preflight check
        swarm_network.status = 'RUNNING'
        swarm_network.save()
        run_nvflare_preflight_check.delay(network_id, user_id)

    except Exception as e:
        if 'swarm_network' in locals():
            swarm_network.status = 'ERROR'
            swarm_network.save()
        print(f"Failed to start network: {e}")


@shared_task
def run_nvflare_preflight_check(network_id, user_id):
    """
    Executes the NVFlare preflight check utility using the admin startup kit.
    This verifies that overseer and server are reachable and certificates are valid.
    """
    try:
        network = SwarmNetwork.objects.get(identifier=network_id)
        user = User.objects.get(id=user_id)
        logger = get_logger(user=user, project=network.project)

        project_name = network.project.title.replace(' ', '_')
        provision_dir = os.path.join(
            settings.BASE_DIR,
            'workspaces',
            str(network.project.identifier),
            str(network.identifier)
        )
        admin_startup_dir = os.path.join(
            provision_dir,
            'workspace',
            project_name,
            'prod_00',
            'admin@nvidia.com',
            'startup'
        )

        if not os.path.exists(admin_startup_dir):
            logger.network.error(
                "Preflight failed: Admin startup kit not found.")
            return

        logger.network.info("Running NVFlare Preflight Check...")

        # Run the preflight check tool
        command = [
            'python3', '-m', 'nvflare.tool.preflight_check',
            '-p', admin_startup_dir
        ]
        result = subprocess.run(command, capture_output=True, text=True)

        # Save output to database logs
        for output in [result.stdout, result.stderr]:
            for line in output.splitlines():
                if line.strip():
                    LogEntry.objects.create(
                        user=user,
                        project=network.project,
                        swarm_network=network,
                        category=LogCategory.NETWORK,
                        source='preflight-check',
                        level='INFO' if output == result.stdout else 'ERROR',
                        message=line.strip()
                    )

        if result.returncode == 0:
            logger.network.info("Preflight check passed.")
        else:
            logger.network.warning("Preflight check found issues. Check logs.")

    except Exception as e:
        print(f"Preflight exception: {e}")


@shared_task
def stop_swarm_network_task(network_id, user_id):
    """
    Stops and removes the containers for a swarm network.
    """
    try:
        network = SwarmNetwork.objects.get(identifier=network_id)
        user = User.objects.get(id=user_id)
        logger = get_logger(user=user, project=network.project)

        project_name = network.project.title.replace(' ', '_')
        provision_dir = os.path.join(
            settings.BASE_DIR,
            'workspaces',
            str(network.project.identifier),
            str(network.identifier)
        )
        compose_dir = os.path.join(
            provision_dir,
            'workspace',
            project_name,
            'prod_00'
        )

        if os.path.exists(os.path.join(compose_dir, 'compose.yaml')):
            logger.network.info(f"Stopping network: {network.name}")
            subprocess.run(
                ['docker-compose', '-f', 'compose.yaml', 'down'],
                cwd=compose_dir
            )
            network.status = 'STOPPED'
            network.save()
            logger.network.info("Network stopped successfully.")
        else:
            network.status = 'ERROR'
            network.save()

    except Exception as e:
        print(f"Failed to stop network: {e}")
