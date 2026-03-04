"""
Celery tasks for the network app.
Handles long-running operations like Docker deployment,
preflight checks, and real-time log streaming.
"""

import os
import json
import stat
import shutil
import subprocess  # nosec B404
from pathlib import Path
from urllib.parse import urlparse

import docker
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


def _docker_network_name(network_identifier):
    return f"swarm_{str(network_identifier)[:12]}_net"


def _container_name_for(network_identifier, participant_name):
    safe_participant = "".join(
        c if c.isalnum() or c in {"-", "_"} else "-"
        for c in str(participant_name)
    ).strip("-")
    return f"swarm-{str(network_identifier)[:12]}-{safe_participant}"[:63]


def _load_json_file(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _ensure_executable(path):
    if not os.path.exists(path):
        return
    current_mode = os.stat(path).st_mode
    os.chmod(path, current_mode | stat.S_IXUSR)


def _extract_host_from_overseer_endpoint(startup_dir):
    fed_client_json = os.path.join(startup_dir, "fed_client.json")
    config = _load_json_file(fed_client_json)

    endpoint = (
        config.get("overseer_agent", {})
        .get("args", {})
        .get("overseer_end_point", "")
    )
    if not endpoint:
        return ""

    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").strip()
    if host in {"", "overseer", "localhost", "127.0.0.1"}:
        return ""
    return host


def _discover_runtime_targets(base_prod_path):
    targets = []
    if not os.path.isdir(base_prod_path):
        return targets

    root_startup = os.path.join(base_prod_path, "startup")
    if os.path.isdir(root_startup):
        fed_server_root = os.path.join(root_startup, "fed_server.json")
        fed_client_root = os.path.join(root_startup, "fed_client.json")
        if os.path.exists(fed_server_root):
            targets.append(
                {
                    "name": "server",
                    "role": "server",
                    "path": base_prod_path,
                    "startup_dir": root_startup,
                }
            )
        elif os.path.exists(fed_client_root):
            fed_client_cfg = _load_json_file(fed_client_root)
            client_name = (
                fed_client_cfg.get("overseer_agent", {})
                .get("args", {})
                .get("name", "client")
            )
            targets.append(
                {
                    "name": str(client_name),
                    "role": "client",
                    "path": base_prod_path,
                    "startup_dir": root_startup,
                }
            )

    for participant in sorted(os.scandir(base_prod_path), key=lambda p: p.name):
        if not participant.is_dir():
            continue

        startup_dir = os.path.join(participant.path, "startup")
        if not os.path.isdir(startup_dir):
            continue

        if participant.name.startswith("admin"):
            continue

        fed_server_path = os.path.join(startup_dir, "fed_server.json")
        fed_client_path = os.path.join(startup_dir, "fed_client.json")

        if os.path.exists(fed_server_path):
            role = "server"
        elif os.path.exists(fed_client_path):
            role = "client"
        elif participant.name == "overseer" or (
            os.path.exists(os.path.join(startup_dir, "gunicorn.conf.py"))
            and os.path.exists(os.path.join(startup_dir, "privilege.yml"))
        ):
            role = "overseer"
        else:
            continue

        targets.append(
            {
                "name": participant.name,
                "role": role,
                "path": participant.path,
                "startup_dir": startup_dir,
            }
        )

    role_order = {"overseer": 0, "server": 1, "client": 2}
    targets.sort(key=lambda t: (role_order.get(t["role"], 9), t["name"]))
    return targets


def _ensure_docker_network(docker_path, network_name, env):
    inspect = subprocess.run(  # nosec B603
        [docker_path, "network", "inspect", network_name],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if inspect.returncode == 0:
        return

    create = subprocess.run(  # nosec B603
        [docker_path, "network", "create", network_name],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if create.returncode != 0:
        raise RuntimeError(
            f"Failed to create Docker network '{network_name}': {create.stderr.strip()}"
        )


def _stop_labeled_runtime(network_id, docker_path, env, logger):
    label = f"swarmcloud.network_id={network_id}"
    result = subprocess.run(  # nosec B603
        [
            docker_path,
            "ps",
            "-a",
            "--filter",
            f"label={label}",
            "--format",
            "{{.ID}} {{.Names}}",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    removed = 0
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if not parts:
                continue
            container_id = parts[0]
            container_name = parts[1] if len(parts) > 1 else container_id

            logger.network.info(f"Stopping container {container_name}...")
            subprocess.run(  # nosec B603
                [docker_path, "stop", "-t", "15", container_id],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            subprocess.run(  # nosec B603
                [docker_path, "rm", "-f", container_id],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            removed += 1

    network_name = _docker_network_name(network_id)
    subprocess.run(  # nosec B603
        [docker_path, "network", "rm", network_name],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    return removed


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
    Deploys a swarm network using NVFlare containerized deployment.
    """
    try:
        swarm_network = SwarmNetwork.objects.get(identifier=network_id)
        user = User.objects.get(id=user_id)
        logger = get_logger(user=user, project=swarm_network.project)

        project_name = slugify(swarm_network.project.title).replace("-", "_")
        provision_dir = os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(swarm_network.project.identifier),
            str(swarm_network.identifier),
        )
        base_prod_path = os.path.join(
            provision_dir, "workspace", project_name, "prod_00"
        )

        targets = _discover_runtime_targets(base_prod_path)
        if not targets:
            raise FileNotFoundError(
                f"No NVFlare startup kits found under: {base_prod_path}"
            )

        logger.network.info(
            f"Discovered startup kits for roles: {[t['role'] for t in targets]}"
        )

        docker_path = shutil.which("docker") or "docker"
        env = os.environ.copy()
        env["DOCKER_BUILDKIT"] = "1"

        image_name = os.getenv(
            "SWARMCLOUD_FLARE_IMAGE", "nvflare/nvflare:2.7.1"
        )
        docker_build_dir = os.path.join(base_prod_path, "nvflare")
        if os.path.exists(os.path.join(docker_build_dir, "Dockerfile")):
            image_name = (
                f"swarmcloud_nvflare_{str(swarm_network.identifier)[:12]}:2.7.1"
            )
            logger.network.info(
                f"Building runtime image for network: {image_name}"
            )
            ret = run_and_log_subprocess(
                [docker_path, "build", "-t", image_name, "."],
                cwd=docker_build_dir,
                env=env,
                logger=logger,
            )
            if ret != 0:
                raise subprocess.CalledProcessError(ret, "docker build")

        network_name = _docker_network_name(swarm_network.identifier)
        _ensure_docker_network(docker_path, network_name, env)

        server_only_mode = (
            os.getenv("SWARMCLOUD_SERVER_ONLY_MODE", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )

        has_overseer_target = any(t["role"] == "overseer" for t in targets)
        filtered_targets = []
        for target in targets:
            if server_only_mode and target["role"] == "client":
                continue
            filtered_targets.append(target)

        logger.network.info(
            f"Starting containerized runtime (server_only_mode={server_only_mode})"
        )

        for target in filtered_targets:
            startup_dir = target["startup_dir"]
            role = target["role"]
            participant_name = target["name"]

            if role in {"client", "server"}:
                _ensure_executable(os.path.join(startup_dir, "sub_start.sh"))
                command = ["/bin/bash", "-lc", "cd /workspace/startup && ./sub_start.sh"]
            else:
                _ensure_executable(os.path.join(startup_dir, "start.sh"))
                command = ["/bin/bash", "-lc", "cd /workspace/startup && ./start.sh"]

            container_name = _container_name_for(
                swarm_network.identifier, participant_name
            )

            subprocess.run(  # nosec B603
                [docker_path, "rm", "-f", container_name],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )

            run_cmd = [
                docker_path,
                "run",
                "-d",
                "--rm",
                "--name",
                container_name,
                "--network",
                network_name,
                "--network-alias",
                participant_name,
                "--label",
                f"swarmcloud.network_id={swarm_network.identifier}",
                "--label",
                f"swarmcloud.role={role}",
                "-v",
                f"{target['path']}:/workspace",
            ]

            if role == "overseer":
                run_cmd.extend(["-p", "8443:8443"])

            if role == "server":
                fed_server_json = _load_json_file(
                    os.path.join(startup_dir, "fed_server.json")
                )
                server_def = (fed_server_json.get("servers") or [{}])[0]
                fed_learn_port = 8002
                admin_port = 8003
                try:
                    service_target = (
                        server_def.get("service", {})
                        .get("target", "server:8002")
                        .split(":")[-1]
                    )
                    fed_learn_port = int(service_target)
                    admin_port = int(server_def.get("admin_port", 8003))
                except Exception:
                    pass

                persist_dir = os.path.join(target["path"], ".nvflare_persist")
                os.makedirs(persist_dir, exist_ok=True)
                run_cmd.extend(
                    [
                        "-v",
                        f"{persist_dir}:/tmp/nvflare",
                        "-p",
                        f"{fed_learn_port}:{fed_learn_port}",
                        "-p",
                        f"{admin_port}:{admin_port}",
                    ]
                )

            if role == "client" and not has_overseer_target:
                remote_host = (
                    os.getenv("SWARMCLOUD_OVERSEER_HOST", "").strip()
                    or (Path(startup_dir) / "server_host.txt").read_text().strip()
                    if os.path.exists(os.path.join(startup_dir, "server_host.txt"))
                    else ""
                )
                if not remote_host:
                    remote_host = _extract_host_from_overseer_endpoint(startup_dir)
                if remote_host:
                    run_cmd.extend(["--add-host", f"overseer:{remote_host}"])
                    run_cmd.extend(["--add-host", f"server:{remote_host}"])

                    aliases_file = os.path.join(startup_dir, "server_aliases.txt")
                    if os.path.exists(aliases_file):
                        try:
                            with open(aliases_file) as af:
                                for alias in af.read().splitlines():
                                    alias = alias.strip()
                                    if alias:
                                        run_cmd.extend(
                                            [
                                                "--add-host",
                                                f"{alias}:{remote_host}",
                                            ]
                                        )
                        except Exception:
                            pass

            run_cmd.extend([image_name] + command)

            logger.network.info(
                f"Starting {role} container: {container_name}"
            )
            run_result = subprocess.run(  # nosec B603
                run_cmd,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            if run_result.returncode != 0:
                raise RuntimeError(
                    f"Failed to start {container_name}: {run_result.stderr.strip()}"
                )

        try:
            logger.network.info(
                f"Connecting app and storage to network: {network_name}"
            )
            subprocess.run(  # nosec B603
                [docker_path, "network", "connect", network_name, "swarmcloud"],
                capture_output=True,
                env=env,
                check=False,
            )
            subprocess.run(  # nosec B603
                [docker_path, "network", "connect", network_name, "minio"],
                capture_output=True,
                env=env,
                check=False,
            )
        except Exception as e:
            logger.network.warning(
                f"Could not connect containers to FLARE network: {e}"
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

        logger.network.info(f"Stopping network: {network.name}")
        docker_path = shutil.which("docker") or "docker"
        env = os.environ.copy()

        stopped = _stop_labeled_runtime(network.identifier, docker_path, env, logger)

        # Legacy fallback for previously compose-based networks.
        compose_path = os.path.join(compose_dir, "compose.yaml")
        if stopped == 0 and os.path.exists(compose_path):
            docker_project_name = f"swarm_{str(network.identifier)[:12]}"
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
            if ret != 0:
                network.status = "ERROR"
                network.save()
                return

        network.status = "STOPPED"
        network.save()
        logger.network.info("Network stopped successfully.")

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

        # 1. Stop Docker containers (containerized runtime first, compose fallback)
        docker_bin = shutil.which("docker") or "docker"
        env = os.environ.copy()
        try:
            removed = _stop_labeled_runtime(network_identifier, docker_bin, env, logger)
            if removed == 0 and os.path.exists(compose_file_path):
                docker_project_name = f"swarm_{network_identifier[:12]}"
                subprocess.run(  # nosec B603
                    [
                        docker_bin,
                        "compose",
                        "-p",
                        docker_project_name,
                        "-f",
                        "compose.yaml",
                        "down",
                    ],
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
