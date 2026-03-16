"""
Sandbox execution utility for MedSwarmHub.
Handles the secure execution of user-provided Python scripts using ephemeral
Docker containers to provide isolation and resource control.
"""

import json
import os
import shutil
import tempfile

import docker
from common.utils import get_docker_client
from django.conf import settings
from logs import logger


def get_host_path(container_path):
    """
    Translates a path inside the container to its absolute path on the 'host'
    (which is the sandbox-dind container itself).
    The project is mounted at /workspace inside sandbox-dind.
    """
    # BASE_DIR is /app inside the django/worker container
    rel_path = os.path.relpath(container_path, settings.BASE_DIR)
    # The sandbox-dind container has the project root at /workspace
    host_path = os.path.join("/workspace", rel_path)
    # Ensure forward slashes for cross-platform compatibility
    return host_path.replace("\\", "/")


def ensure_sandbox_image():
    """
    Checks if the 'medswarmhub-sandbox' image exists locally on the sandbox daemon.
    If not, it builds it from the Dockerfile.sandbox in the project root.
    """
    log = logger.get_logger()
    client = get_docker_client(target="sandbox")

    try:
        client.images.get("medswarmhub-sandbox")
    except docker.errors.ImageNotFound:
        log.data.info(
            "Sandbox image not found. Building 'medswarmhub-sandbox' "
            "automatically (this may take a few minutes)..."
        )
        dockerfile_path = os.path.join(settings.BASE_DIR, "Dockerfile.sandbox")
        if not os.path.exists(dockerfile_path):
            log.data.error(
                f"Dockerfile.sandbox not found at {dockerfile_path}"
            )
            raise FileNotFoundError(
                "Dockerfile.sandbox is missing. Cannot build sandbox."
            )

        # Build the image and stream logs
        try:
            generator = client.api.build(
                path=str(settings.BASE_DIR),
                dockerfile="Dockerfile.sandbox",
                tag="medswarmhub-sandbox",
                rm=True,
                decode=True,
            )

            for chunk in generator:
                if "stream" in chunk:
                    # Log build progress to console
                    print(chunk["stream"].strip())
                elif "error" in chunk:
                    log.data.error(f"Docker build error: {chunk['error']}")
                    raise docker.errors.BuildError(
                        chunk["error"], generator
                    ) from None

            log.data.info("Sandbox image built successfully.")
        except Exception as e:
            log.data.error(f"Failed to build sandbox image: {e}")
            raise RuntimeError(f"Sandbox build failed: {e}") from e


def ensure_sandbox_network():
    """
    Ensures that the 'sandbox_internal' network exists in the sandbox daemon.
    We make it an internal bridge network (internal=True) to prevent data exfiltration.
    Containers can still reach the host gateway to talk to MinIO if the daemon
    is configured to allow it, but outbound internet access is blocked.
    """
    log = logger.get_logger()
    client = get_docker_client(target="sandbox")

    try:
        net = client.networks.get("sandbox_internal")
        # If the existing network is NOT internal, we recreate it for security.
        if not net.attrs.get("Internal", False):
            log.data.info("Recreating 'sandbox_internal' network as internal for security...")
            net.remove()
            raise docker.errors.NotFound("Recreating")
    except docker.errors.NotFound:
        log.data.info("Creating 'sandbox_internal' network (internal=True) in sandbox...")
        try:
            client.networks.create(
                "sandbox_internal",
                driver="bridge",
                internal=True,
                check_duplicate=True,
            )
        except Exception as e:
            log.data.error(f"Failed to create sandbox network: {e}")
            raise


def run_script_in_sandbox(
    script_content, data_dir, project_uuid, run_type="validation"
):
    """
    Runs a Python script inside an ephemeral Docker container on the isolated daemon.
    """
    log = logger.get_logger()
    ensure_sandbox_image()
    ensure_sandbox_network()

    client = get_docker_client(target="sandbox")

    # Create a unique temporary directory within the project root for this execution
    # This ensures the directory is visible to the host Docker daemon via existing mounts.
    sandbox_dir = tempfile.mkdtemp(
        prefix=f"sandbox_{run_type}_{project_uuid}_",
        dir=settings.PROJECT_TEMP_DIR,
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
            host_data_path: {"bind": "/home/sandboxuser/data", "mode": "ro"},
            host_run_path: {"bind": "/home/sandboxuser/run", "mode": "rw"},
        }

        # Resolve 'minio' IP to pass to the sandbox container
        import socket
        try:
            minio_ip = socket.gethostbyname("minio")
            extra_hosts = {"minio": minio_ip}
        except Exception as e:
            log.data.warning(f"Could not resolve 'minio' IP for sandbox: {e}")
            extra_hosts = {}

        container = None
        try:
            # Enable GPU if requested and available on the daemon
            device_requests = []
            gpu_enabled = (
                os.getenv("MEDSWARMHUB_ENABLE_GPU", "false").strip().lower()
                in {"1", "true", "yes", "on"}
            )
            if gpu_enabled:
                try:
                    info = client.info()
                    runtimes = info.get("Runtimes", {})
                    # Check if 'nvidia' runtime is available
                    if "nvidia" in runtimes:
                        device_requests.append(
                            docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
                        )
                    else:
                        log.data.warning(
                            "GPU was requested but 'nvidia' runtime is not available on the sandbox daemon. "
                            "Falling back to CPU."
                        )
                except Exception as e:
                    log.data.warning(f"Could not check for GPU support: {e}. Falling back to CPU.")

            # Run the container with resource limits.
            # We use the 'sandbox_internal' bridge network for isolation.
            # This allows the container to reach the host gateway (for MinIO)
            # without exposing the host network namespace to the user script.
            # We inject the 'minio' IP via extra_hosts so the sandbox can resolve it.
            container = client.containers.run(
                image="medswarmhub-sandbox",
                command=["script.py"],
                volumes=volumes,
                working_dir="/home/sandboxuser/run",
                network="sandbox_internal",
                extra_hosts=extra_hosts,
                mem_limit="1g",
                nano_cpus=1000000000,  # 1 CPU
                shm_size="10.24gb",
                device_requests=device_requests,
                detach=True,
                stdout=True,
                stderr=True,
                remove=False,
            )

            # Wait for completion (max 5 minutes)
            result = container.wait(timeout=300)
            exit_code = result.get("StatusCode", 1)

            logs = container.logs().decode("utf-8")

            # Read results.json if it was created by the helper
            results_data = {}
            if os.path.exists(results_path):
                try:
                    with open(results_path) as f:
                        results_data = json.load(f)
                except Exception as e:
                    log.data.warning(f"Failed to parse results.json: {e}")

            # Capture plots if it's a visualization run
            captured_plots = []
            if run_type in ["visualization", "results_visualization"]:
                for plot_file in sorted(os.listdir(plots_dir)):
                    if plot_file.endswith(".json"):
                        try:
                            with open(
                                os.path.join(plots_dir, plot_file)
                            ) as f:
                                captured_plots.append(json.load(f))
                        except Exception as e:
                            log.data.warning(
                                f"Failed to load plot data from {plot_file}: {e}"
                            )

            return {
                "success": exit_code == 0,
                "output": logs,
                "exit_code": exit_code,
                "results": (
                    results_data.get("checks", [])
                    if run_type == "validation"
                    else results_data
                ),
                "plots": captured_plots,
            }

        except Exception as e:
            log.data.error(f"Sandbox execution failed: {e}")
            return {"success": False, "output": str(e), "error": str(e)}
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
