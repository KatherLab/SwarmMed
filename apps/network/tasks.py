"""
Celery tasks for the network app.
Handles long-running operations like Docker deployment,
preflight checks, and real-time log streaming.
"""

import os
import shutil
import subprocess  # nosec B404

import docker
import yaml
from celery import shared_task
from django.conf import settings
from django.contrib.auth.models import User
from django.utils.text import slugify
from logs.logger import get_logger
from logs.models import LogCategory, LogEntry
from project.models import Project

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
        bufsize=1,
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
    self, container_name, command, network_id, project_id, user_id
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
                level="INFO",
                message=line.decode("utf-8").strip(),
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
        project_name = slugify(swarm_network.project.title).replace("-", "_")
        provision_dir = os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(swarm_network.project.identifier),
            str(swarm_network.identifier),
        )
        compose_dir = os.path.join(
            provision_dir, "workspace", project_name, "prod_00"
        )
        compose_file_path = os.path.join(compose_dir, "compose.yaml")

        if not os.path.exists(compose_file_path):
            raise FileNotFoundError(
                f"Compose file missing: {compose_file_path}"
            )

        # 1. Modify compose file for host-path mapping and platform enforcement
        with open(compose_file_path) as f:
            compose_content = yaml.safe_load(f)

        # Security Validation: Prevent unauthorized host-bind mounts and privileged mode
        if "services" in compose_content:
            for service_name, service_config in compose_content[
                "services"
            ].items():
                if service_config.get("privileged"):
                    raise ValueError(
                        f"Security error: Privileged mode is not allowed for service '{service_name}'"
                    )

                volumes = service_config.get("volumes", [])
                for volume in volumes:
                    if isinstance(volume, str):
                        # Short syntax: host:container
                        if ":" in volume:
                            host_path = volume.split(":")[0]
                            # Allow relative paths starting with ./ or just file names
                            # Deny absolute paths and paths going up too far
                            if host_path.startswith("/") or ".." in host_path:
                                # Allow paths that are within the project root (e.g. modified by previous run)
                                allowed_root = os.getenv(
                                    "HOST_PROJECT_PATH", str(settings.BASE_DIR)
                                )
                                if host_path.startswith(allowed_root):
                                    continue

                                if not any(
                                    host_path.startswith(allowed)
                                    for allowed in [
                                        "./fl-client",
                                        "./server",
                                        "./overseer",
                                        "./nvflare",
                                    ]
                                ):
                                    raise ValueError(
                                        f"Security error: Unauthorized host-bind mount '{host_path}' in service '{service_name}'"
                                    )
                    elif isinstance(volume, dict):
                        # Long syntax
                        if volume.get("type") == "bind":
                            source = volume.get("source", "")
                            if source.startswith("/") or ".." in source:
                                raise ValueError(
                                    f"Security error: Unauthorized host-bind mount '{source}' in service '{service_name}'"
                                )

        # Write back YAML structure first
        with open(compose_file_path, "w") as f:
            yaml.safe_dump(compose_content, f, default_flow_style=False)

        # Then perform text-based host path replacement if needed
        host_project_path = os.getenv("HOST_PROJECT_PATH")
        if host_project_path:
            with open(compose_file_path) as f:
                content = f.read()

            rel_dir = os.path.relpath(compose_dir, settings.BASE_DIR)
            host_dir = os.path.join(host_project_path, rel_dir)

            mappings = {
                "build: ./nvflare": f"build: {os.path.join(host_dir, 'nvflare')}",
                "./fl-client": os.path.join(host_dir, "fl-client"),
                "./server": os.path.join(host_dir, "server"),
                "./overseer": os.path.join(host_dir, "overseer"),
                "./nvflare": os.path.join(host_dir, "nvflare"),
                "./:": f"{host_dir}:",
            }
            for old, new in mappings.items():
                content = content.replace(old, new)

            with open(compose_file_path, "w") as f:
                f.write(content)
            logger.network.info(
                "Updated compose file with host paths and platform enforcement."
            )

        # 2. Build Docker images with live logging
        logger.network.info("Building Docker images for the network...")
        docker_path = shutil.which("docker") or "docker"
        env = os.environ.copy()

        # Use BuildKit for better compatibility and efficiency
        env["DOCKER_BUILDKIT"] = "1"
        env["COMPOSE_DOCKER_CLI_BUILD"] = "1"

        # Use unique project name to avoid clashes
        docker_project_name = f"swarm_{str(swarm_network.identifier)[:12]}"

        ret = run_and_log_subprocess(
            [
                docker_path,
                "compose",
                "-p",
                docker_project_name,
                "-f",
                "compose.yaml",
                "build",
            ],
            cwd=compose_dir,
            env=env,
            logger=logger,
        )
        if ret != 0:
            raise subprocess.CalledProcessError(ret, "docker compose build")

        # 3. Start containers with live logging
        # Determine which services to start:
        # - Default: start ALL services from compose.yaml (works for local testing out of the box).
        # - Optional server-only mode: if explicitly enabled, start ONLY server-like + overseer.
        services_to_start = []
        available_services = compose_content.get("services", {})

        server_only_mode = (
            os.getenv("SWARMCLOUD_SERVER_ONLY_MODE", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )

        if server_only_mode:
            server_like_services = sorted(
                [
                    service_name
                    for service_name in available_services.keys()
                    if str(service_name) == "server"
                    or str(service_name).startswith("server-")
                ]
            )
            if server_like_services:
                services_to_start.extend(server_like_services)
                if "overseer" in available_services:
                    services_to_start.append("overseer")

        command = [
            docker_path,
            "compose",
            "-p",
            docker_project_name,
            "-f",
            "compose.yaml",
            "up",
            "-d",
        ]
        
        if services_to_start:
            command.extend(services_to_start)

        logger.network.info(
            f"Starting Docker containers (detached): {services_to_start or 'ALL'} "
            f"(server_only_mode={server_only_mode})..."
        )
        ret = run_and_log_subprocess(
            command,
            cwd=compose_dir,
            env=env,
            logger=logger,
        )
        if ret != 0:
            raise subprocess.CalledProcessError(ret, "docker compose up")

        # 4. Network connection
        try:
            # Docker Compose adds '_default' to the project name for its default network
            net_name = f"{docker_project_name}_default"
            logger.network.info(
                f"Connecting app and storage to network: {net_name}"
            )

            # Connect the main web app
            subprocess.run(  # nosec B603
                [docker_path, "network", "connect", net_name, "swarmcloud"],
                capture_output=True,
                env=env,
                check=False,  # Might already be connected
            )

            # Connect the MinIO storage container
            subprocess.run(  # nosec B603
                [docker_path, "network", "connect", net_name, "minio"],
                capture_output=True,
                env=env,
                check=False,  # Might already be connected
            )
        except Exception as e:
            logger.network.warning(
                f"Could not connect containers to flare network: {e}"
            )

        # Mark as running and trigger preflight check
        swarm_network.status = "RUNNING"
        swarm_network.save()
        run_nvflare_preflight_check.delay(network_id, user_id)

    except Exception as e:
        if "swarm_network" in locals():
            swarm_network.status = "ERROR"
            swarm_network.save()

        # Ensure the error is logged to the database so the user sees it
        internal_logger = get_logger(
            user=User.objects.get(id=user_id),
            project=(
                swarm_network.project if "swarm_network" in locals() else None
            ),
        )
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

        project_name = slugify(network.project.title).replace("-", "_")
        provision_dir = os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(network.project.identifier),
            str(network.identifier),
        )
        admin_startup_dir = os.path.join(
            provision_dir,
            "workspace",
            project_name,
            "prod_00",
            "admin@nvidia.com",
            "startup",
        )

        if not os.path.exists(admin_startup_dir):
            prod_dir = os.path.join(
                provision_dir,
                "workspace",
                project_name,
                "prod_00",
            )
            if os.path.exists(prod_dir):
                admin_dirs = sorted(
                    [
                        os.path.join(prod_dir, d, "startup")
                        for d in os.listdir(prod_dir)
                        if d.startswith("admin-")
                        and os.path.isdir(os.path.join(prod_dir, d, "startup"))
                    ]
                )
                if admin_dirs:
                    admin_startup_dir = admin_dirs[0]

        if not os.path.exists(admin_startup_dir):
            logger.network.info(
                "Skipping Preflight Check: Admin startup kit not found (Expected for Client Nodes)."
            )
            return

        logger.network.info("Running NVFlare Preflight Check...")

        python_path = shutil.which("python3") or "python3"
        command = [
            python_path,
            "-m",
            "nvflare.tool.preflight_check",
            "-p",
            admin_startup_dir,
        ]

        env = os.environ.copy()
        result = subprocess.run(
            command, capture_output=True, text=True, env=env
        )  # nosec B603

        for output in [result.stdout, result.stderr]:
            for line in output.splitlines():
                if line.strip():
                    LogEntry.objects.create(
                        user=user,
                        project=network.project,
                        swarm_network=network,
                        category=LogCategory.NETWORK,
                        source="preflight-check",
                        level="INFO" if output == result.stdout else "ERROR",
                        message=line.strip(),
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

        project_name = slugify(network.project.title).replace("-", "_")
        provision_dir = os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(network.project.identifier),
            str(network.identifier),
        )
        compose_dir = os.path.join(
            provision_dir, "workspace", project_name, "prod_00"
        )

        if os.path.exists(os.path.join(compose_dir, "compose.yaml")):
            logger.network.info(f"Stopping network: {network.name}")
            docker_path = shutil.which("docker") or "docker"

            docker_project_name = f"swarm_{str(network.identifier)[:12]}"

            env = os.environ.copy()
            ret = run_and_log_subprocess(
                [
                    docker_path,
                    "compose",
                    "-p",
                    docker_project_name,
                    "-f",
                    "compose.yaml",
                    "down",
                ],
                cwd=compose_dir,
                env=env,
                logger=logger,
            )
            if ret == 0:
                network.status = "STOPPED"
                network.save()
                logger.network.info("Network stopped successfully.")
            else:
                network.status = "ERROR"
                network.save()
        else:
            network.status = "ERROR"
            network.save()

    except Exception as e:
        print(f"Failed to stop network: {e}")


@shared_task
def stop_and_delete_network_task(network_id):
    """
    Stops a running network and then deletes its database record.
    This ensures that resources are cleaned up before the record is gone.
    """
    try:
        network = SwarmNetwork.objects.get(identifier=network_id)

        # 1. Perform cleanup (stops containers, syncs results, and removes files)
        # We call the cleanup function synchronously within this task
        cleanup_network_resources(
            project_title=network.project.title,
            project_identifier=str(network.project.identifier),
            network_identifier=str(network.identifier),
            network_name=network.name,
        )

        # 2. Delete the record from the database
        # We use filter().delete() to avoid re-triggering the custom delete() method
        SwarmNetwork.objects.filter(identifier=network_id).delete()

    except SwarmNetwork.DoesNotExist:
        # If it was already deleted, we just ensure resources are gone
        # (Though we don't have the details here, cleanup_network_resources
        # might have already been called by another task)
        pass
    except Exception as e:
        internal_logger = get_logger()
        internal_logger.network.error(
            f"Failed to stop and delete network {network_id}: {e}"
        )


@shared_task
def cleanup_network_resources(
    project_title, project_identifier, network_identifier, network_name
):
    """
    Asynchronously cleans up Docker containers and filesystem resources
    associated with a deleted SwarmNetwork.

    Arguments are passed as strings since the DB record might already be deleted.
    """
    try:
        # Use a generic system logger since the network/project might be gone
        logger = get_logger()

        project_name_slug = slugify(project_title).replace("-", "_")

        provision_dir = os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(project_identifier),
            str(network_identifier),
        )

        compose_dir = os.path.join(
            provision_dir, "workspace", project_name_slug, "prod_00"
        )

        compose_file_path = os.path.join(compose_dir, "compose.yaml")

        # 1. Stop Docker containers
        if os.path.exists(compose_file_path):
            # Handle host path adjustment for cleanup if needed
            host_project_path = os.getenv("HOST_PROJECT_PATH")
            if host_project_path:
                try:
                    with open(compose_file_path) as f:
                        f.read()

                    relative_compose_dir = os.path.relpath(
                        compose_dir, settings.BASE_DIR
                    )
                    host_compose_dir = os.path.join(
                        host_project_path, relative_compose_dir
                    )

                    {
                        "build: ./nvflare": f"build: {os.path.join(host_compose_dir, 'nvflare')}",
                        "./fl-client": os.path.join(
                            host_compose_dir, "fl-client"
                        ),
                        "./server": os.path.join(host_compose_dir, "server"),
                        "./overseer": os.path.join(
                            host_compose_dir, "overseer"
                        ),
                    }

                    # We need to make sure we are tearing down the *same* configuration
                    # that was brought up. If the file on disk already has host paths,
                    # simply running 'down' is fine. If we need to modify it to match, we do so.
                    # Usually, the file on disk should be the one last used.

                except Exception as e:
                    logger.network.warning(
                        f"Error reading compose file during cleanup prep: {e}"
                    )

            # Reliable binary detection
            docker_compose_bin = shutil.which("docker-compose")
            if docker_compose_bin:
                cmd = [docker_compose_bin]
            else:
                docker_bin = shutil.which("docker")
                if docker_bin:
                    cmd = [docker_bin, "compose"]
                else:
                    cmd = ["docker", "compose"]  # Fallback

            # Use unique project name to ensure we target the right containers
            docker_project_name = f"swarm_{network_identifier[:12]}"
            cmd.extend(
                ["-p", docker_project_name, "-f", "compose.yaml", "down"]
            )

            logger.network.info(
                f"Stopping Docker resources for deleted network: {network_name}"
            )
            try:
                subprocess.run(  # nosec B603
                    cmd,
                    cwd=compose_dir,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                logger.network.error(
                    f"Failed to stop Docker containers for deleted network {network_name}: {e}"
                )

        # 2. Sync results to S3 before wiping the local filesystem
        try:
            from results.tasks import sync_project_results

            logger.network.info(
                f"Synchronizing results for network {network_name} before deletion."
            )
            # Run synchronously to ensure files are uploaded before rmtree
            sync_project_results(project_identifier)
        except Exception as e:
            logger.network.error(
                f"Result sync failed during network cleanup for {network_name}: {e}"
            )

        # 3. Cleanup Filesystem
        if os.path.exists(provision_dir):
            shutil.rmtree(provision_dir)
            logger.network.info(
                f"Cleaned up workspace directory for deleted network: {network_name}"
            )

    except Exception as e:
        logger.network.error(
            f"Error during network resource cleanup for {network_name}: {e}"
        )
