"""
Sandbox execution utility for SwarmCloud.
Handles the secure execution of user-provided Python scripts using ephemeral
Docker containers to provide isolation and resource control.
"""

import os
import json
import shutil
import tempfile
import docker
from django.conf import settings
from apps.logs import logger


def get_docker_client():
    """
    Returns a Docker client configured to use the proxy if DOCKER_HOST is set,
    otherwise falls back to the local socket.
    """
    docker_host = os.environ.get("DOCKER_HOST")
    if docker_host:
        return docker.DockerClient(base_url=docker_host)
    return docker.from_env()


def get_host_path(container_path):
    """
    Translates a path inside the container to its absolute path on the host.
    This is required for Docker volume mounting when running 'Docker-in-Docker'.
    """
    # BASE_DIR is /app inside the container
    rel_path = os.path.relpath(container_path, settings.BASE_DIR)
    # Join the host's project root with the relative path
    host_path = os.path.join(settings.HOST_PROJECT_PATH, rel_path)
    # Ensure forward slashes for cross-platform compatibility
    return host_path.replace('\\', '/')


def ensure_sandbox_image():
    """
    Checks if the 'swarmcloud-sandbox' image exists locally.
    If not, it builds it from the Dockerfile.sandbox in the project root.
    """
    log = logger.get_logger()
    client = get_docker_client()

    try:
        client.images.get("swarmcloud-sandbox")
    except docker.errors.ImageNotFound:
        log.data.info(
            "Sandbox image not found. Building 'swarmcloud-sandbox' "
            "automatically (this may take a few minutes)..."
        )
        dockerfile_path = os.path.join(settings.BASE_DIR, "Dockerfile.sandbox")
        if not os.path.exists(dockerfile_path):
            log.data.error(f"Dockerfile.sandbox not found at {dockerfile_path}")
            raise FileNotFoundError(
                "Dockerfile.sandbox is missing. Cannot build sandbox."
            )

        # Build the image and stream logs
        try:
            generator = client.api.build(
                path=str(settings.BASE_DIR),
                dockerfile="Dockerfile.sandbox",
                tag="swarmcloud-sandbox",
                rm=True,
                decode=True
            )

            for chunk in generator:
                if 'stream' in chunk:
                    # Log build progress to console
                    print(chunk['stream'].strip())
                elif 'error' in chunk:
                    log.data.error(f"Docker build error: {chunk['error']}")
                    raise docker.errors.BuildError(chunk['error'], generator)

            log.data.info("Sandbox image built successfully.")
        except Exception as e:
            log.data.error(f"Failed to build sandbox image: {e}")
            raise


def run_script_in_sandbox(script_content, data_dir, project_uuid,
                          run_type="validation"):
    """
    Runs a Python script inside an ephemeral Docker container.
    """
    log = logger.get_logger()
    ensure_sandbox_image()

    client = get_docker_client()

    # Create a unique temporary directory within the project root for this execution
    # This ensures the directory is visible to the host Docker daemon via existing mounts.
    sandbox_dir = tempfile.mkdtemp(
        prefix=f"sandbox_{run_type}_{project_uuid}_",
        dir=settings.PROJECT_TEMP_DIR
    )

    try:
        # Paths inside the sandbox directory (container side)
        script_path = os.path.join(sandbox_dir, "script.py")
        results_path = os.path.join(sandbox_dir, "results.json")
        plots_dir = os.path.join(sandbox_dir, "plots")
        os.makedirs(plots_dir, exist_ok=True)

        # Write the script to the sandbox directory
        with open(script_path, "w") as f:
            f.write(script_content)

        # Define volume mounts using HOST-SIDE paths
        # 1. Translate data_dir (downloads) to host path
        host_data_path = get_host_path(data_dir)
        # 2. Translate sandbox_dir (run workspace) to host path
        host_run_path = get_host_path(sandbox_dir)

        volumes = {
            host_data_path: {'bind': '/home/sandboxuser/data', 'mode': 'ro'},
            host_run_path: {'bind': '/home/sandboxuser/run', 'mode': 'rw'}
        }

        container = None
        try:
            # Run the container with resource limits and no network access
            # We only pass ["script.py"] because "python" is the ENTRYPOINT in Dockerfile.sandbox
            container = client.containers.run(
                image="swarmcloud-sandbox",
                command=["script.py"],
                volumes=volumes,
                working_dir="/home/sandboxuser/run",
                network_disabled=True,
                mem_limit="1g",
                nano_cpus=1000000000,  # 1 CPU
                detach=True,
                stdout=True,
                stderr=True,
                remove=False  # We want to check status before removal
            )

            # Wait for completion (max 5 minutes)
            result = container.wait(timeout=300)
            exit_code = result.get('StatusCode', 1)

            logs = container.logs().decode('utf-8')

            # Read results.json if it was created by the helper
            results_data = {}
            if os.path.exists(results_path):
                try:
                    with open(results_path, "r") as f:
                        results_data = json.load(f)
                except Exception as e:
                    log.data.warning(f"Failed to parse results.json: {e}")

            # Capture plots if it's a visualization run
            captured_plots = []
            if run_type in ["visualization", "results_visualization"]:
                for plot_file in sorted(os.listdir(plots_dir)):
                    if plot_file.endswith(".json"):
                        try:
                            with open(os.path.join(plots_dir, plot_file),
                                      "r") as f:
                                captured_plots.append(json.load(f))
                        except Exception as e:
                            log.data.warning(
                                f"Failed to load plot data from {plot_file}: {e}"
                            )

            return {
                'success': exit_code == 0,
                'output': logs,
                'exit_code': exit_code,
                'results': results_data.get('checks', []) if run_type == "validation" else results_data,
                'plots': captured_plots
            }

        except Exception as e:
            log.data.error(f"Sandbox execution failed: {e}")
            return {
                'success': False,
                'output': str(e),
                'error': str(e)
            }
        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception as e:
                    # Best effort removal
                    log.data.warning(
                        f"Failed to remove sandbox container: {e}"
                    )
    finally:
        # Clean up the temporary workspace
        try:
            shutil.rmtree(sandbox_dir)
        except Exception as e:
            log.data.warning(
                f"Failed to cleanup sandbox directory {sandbox_dir}: {e}"
            )
