"""
Celery tasks for the network app.
Handles long-running operations like Docker deployment,
preflight checks, and real-time log streaming.
"""

import os
import subprocess  # nosec B404
import shutil
import yaml

import docker
from celery import shared_task
from django.conf import settings
from django.contrib.auth.models import User
from django.utils.text import slugify

from apps.logs.logger import get_logger
from apps.logs.models import LogCategory, LogEntry
from apps.project.models import Project
from .models import SwarmNetwork


def run_and_log_subprocess(command, cwd, env, logger):
    """
    Executes a subprocess and streams its output to the provided logger.
    """
    process = subprocess.Popen(  # nosec B603
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    for line in process.stdout:
        line = line.strip()
        if line:
            # Use the network category for these logs
            logger.network.info(line)

    process.wait()
    return process.returncode


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

        # Connect via DOCKER_HOST if provided (e.g. for the proxy), else use local socket
        docker_host = os.environ.get("DOCKER_HOST", None)
        client = docker.from_env() if not docker_host else docker.DockerClient(base_url=docker_host)
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
    """
    try:
        swarm_network = SwarmNetwork.objects.get(identifier=network_id)
        user = User.objects.get(id=user_id)
        logger = get_logger(user=user, project=swarm_network.project)

        # Sanitize project name to prevent path traversal
        project_name = slugify(swarm_network.project.title).replace('-', '_')
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

        # 1. Modify compose file for host-path mapping and platform enforcement
        with open(compose_file_path, 'r') as f:
            compose_content = yaml.safe_load(f)

        # Security Validation: Prevent unauthorized host-bind mounts and privileged mode
        if 'services' in compose_content:
            for service_name, service_config in compose_content['services'].items():
                if service_config.get('privileged'):
                    raise ValueError(f"Security error: Privileged mode is not allowed for service '{service_name}'")

                volumes = service_config.get('volumes', [])
                for volume in volumes:
                    if isinstance(volume, str):
                        # Short syntax: host:container
                        if ':' in volume:
                            host_path = volume.split(':')[0]
                            # Allow relative paths starting with ./ or just file names
                            # Deny absolute paths and paths going up too far
                            if host_path.startswith('/') or '..' in host_path:
                                if not any(host_path.startswith(allowed) for allowed in ['./fl-client', './server', './overseer', './nvflare']):
                                    raise ValueError(f"Security error: Unauthorized host-bind mount '{host_path}' in service '{service_name}'")
                    elif isinstance(volume, dict):
                        # Long syntax
                        if volume.get('type') == 'bind':
                            source = volume.get('source', '')
                            if source.startswith('/') or '..' in source:
                                raise ValueError(f"Security error: Unauthorized host-bind mount '{source}' in service '{service_name}'")

        # Write back YAML structure first
        with open(compose_file_path, 'w') as f:
            yaml.safe_dump(compose_content, f, default_flow_style=False)

        # Then perform text-based host path replacement if needed
        host_project_path = os.getenv('HOST_PROJECT_PATH')
        if host_project_path:
            with open(compose_file_path, 'r') as f:
                content = f.read()

            rel_dir = os.path.relpath(compose_dir, settings.BASE_DIR)
            host_dir = os.path.join(host_project_path, rel_dir)

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
            logger.network.info("Updated compose file with host paths and platform enforcement.")

        # 2. Build Docker images with live logging
        logger.network.info("Building Docker images for the network...")
        docker_path = shutil.which('docker') or 'docker'
        env = os.environ.copy()

        # Use BuildKit for better compatibility and efficiency
        env["DOCKER_BUILDKIT"] = "1"
        env["COMPOSE_DOCKER_CLI_BUILD"] = "1"

        ret = run_and_log_subprocess(
            [docker_path, 'compose', '-f', 'compose.yaml', 'build'],
            cwd=compose_dir,
            env=env,
            logger=logger
        )
        if ret != 0:
            raise subprocess.CalledProcessError(ret, "docker compose build")

        # 3. Start containers with live logging
        logger.network.info("Starting Docker containers (detached)...")
        ret = run_and_log_subprocess(
            [docker_path, 'compose', '-f', 'compose.yaml', 'up', '-d'],
            cwd=compose_dir,
            env=env,
            logger=logger
        )
        if ret != 0:
            raise subprocess.CalledProcessError(ret, "docker compose up")

        # 4. Network connection
        try:
            net_name = os.path.basename(compose_dir) + "_default"
            logger.network.info(f"Connecting app and storage to network: {net_name}")

            # Connect via DOCKER_HOST if provided (e.g. for the proxy), else use local socket
            docker_host = os.environ.get("DOCKER_HOST", None)
            client = docker.from_env() if not docker_host else docker.DockerClient(base_url=docker_host)

            try:
                network = client.networks.get(net_name)

                # Connect the main web app
                try:
                    network.connect("swarmcloud")
                except Exception:
                    pass # Might already be connected

                # Connect the MinIO storage container
                try:
                    network.connect("minio")
                except Exception:
                    pass # Might already be connected

            except docker.errors.NotFound:
                logger.network.warning(f"Network {net_name} not found.")

        except Exception as e:
            logger.network.warning(f"Could not connect containers to flare network: {e}")

        # Mark as running and trigger preflight check
        swarm_network.status = 'RUNNING'
        swarm_network.save()
        run_nvflare_preflight_check.delay(network_id, user_id)

    except Exception as e:
        if 'swarm_network' in locals():
            swarm_network.status = 'ERROR'
            swarm_network.save()

        # Ensure the error is logged to the database so the user sees it
        internal_logger = get_logger(user=User.objects.get(id=user_id),
                                     project=swarm_network.project if 'swarm_network' in locals() else None)
        internal_logger.network.error(f"Failed to start network: {str(e)}")


@shared_task
def run_nvflare_preflight_check(network_id, user_id):
    """
    Executes the NVFlare preflight check utility using the admin startup kit.
    """
    try:
        network = SwarmNetwork.objects.get(identifier=network_id)
        user = User.objects.get(id=user_id)
        logger = get_logger(user=user, project=network.project)

        project_name = slugify(network.project.title).replace('-', '_')
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
            logger.network.error("Preflight failed: Admin startup kit not found.")
            return

        logger.network.info("Running NVFlare Preflight Check...")

        python_path = shutil.which('python3') or 'python3'
        command = [
            python_path, '-m', 'nvflare.tool.preflight_check',
            '-p', admin_startup_dir
        ]

        env = os.environ.copy()
        result = subprocess.run(command, capture_output=True, text=True, env=env)  # nosec B603

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

        project_name = slugify(network.project.title).replace('-', '_')
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
            docker_path = shutil.which('docker') or 'docker'

            env = os.environ.copy()
            ret = run_and_log_subprocess(
                [docker_path, 'compose', '-f', 'compose.yaml', 'down'],
                cwd=compose_dir,
                env=env,
                logger=logger
            )
            if ret == 0:
                network.status = 'STOPPED'
                network.save()
                logger.network.info("Network stopped successfully.")
            else:
                network.status = 'ERROR'
                network.save()
        else:
            network.status = 'ERROR'
            network.save()

    except Exception as e:
        print(f"Failed to stop network: {e}")
