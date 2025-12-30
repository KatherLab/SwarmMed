from celery import shared_task
import docker
from apps.logs.models import LogEntry, LogCategory, SwarmNetwork
from apps.project.models import Project
from apps.logs.logger import get_logger
import os
import subprocess
import yaml
from django.contrib.auth.models import User
from django.conf import settings

@shared_task(bind=True)
def execute_and_log_in_container(self, container_name: str, command: str, network_id: str, project_id: str, user_id: int):
    """
    Executes a command in a container and streams its logs to the LogEntry model.
    """
    logger = get_logger()
    try:
        network = SwarmNetwork.objects.get(identifier=network_id)
        project = Project.objects.get(identifier=project_id)
        logger = get_logger(project=project)
        client = docker.from_env()
        container = client.containers.get(container_name)

        logger.network.info(f"Executing command in {container_name}: {command}")

        exec_result = container.exec_run(command, stream=True)
        
        for line in exec_result.output:
            LogEntry.objects.create(
                user_id=user_id,
                project=project,
                swarm_network=network,
                category=LogCategory.NETWORK,
                source=container_name,
                level='INFO',
                message=line.decode('utf-8').strip()
            )

    except SwarmNetwork.DoesNotExist:
        logger.network.error(f"ERROR: SwarmNetwork {network_id} not found when executing command.")
    except docker.errors.NotFound:
        logger.network.error(f"ERROR: Container {container_name} not found for command execution.")
    except Exception as e:
        logger.network.critical(f"CRITICAL: An error occurred in command executor for {container_name}: {e}")
        try:
            if 'network' in locals():
                LogEntry.objects.create(
                    user_id=user_id,
                    project=network.project,
                    swarm_network=network,
                    category=LogCategory.NETWORK,
                    source=container_name,
                    level='ERROR',
                    message=f"Command executor for {container_name} crashed: {e}"
                )
        except Exception as log_e:
            logger.network.critical(f"CRITICAL: Failed to log exception to DB. Main error: {e}, Logging error: {log_e}")

@shared_task
def start_swarm_network_task(network_id, user_id):
    swarm_network = SwarmNetwork.objects.get(identifier=network_id)
    user = User.objects.get(id=user_id)
    logger = get_logger(user=user, project=swarm_network.project)
    project_name = swarm_network.project.title.replace(' ', '_')
    provision_dir = os.path.join(settings.BASE_DIR, 'workspaces', str(swarm_network.project.identifier), str(swarm_network.identifier))
    compose_dir = os.path.join(provision_dir, 'workspace', project_name, 'prod_00')
    compose_file_path = os.path.join(compose_dir, 'compose.yaml')

    # Extra logging
    logger.network.info(f"Checking for files in: {compose_dir}")
    try:
        files_in_dir = os.listdir(compose_dir)
        logger.network.info(f"Files found: {files_in_dir}")
    except FileNotFoundError:
        logger.network.error(f"Directory not found: {compose_dir}")

    logger.network.info(f"Looking for compose file at: {compose_file_path}")
    if os.path.exists(compose_file_path):
        host_project_path = os.getenv('HOST_PROJECT_PATH')
        if host_project_path:
            with open(compose_file_path, 'r') as f:
                compose_content = f.read()

            relative_compose_dir = os.path.relpath(compose_dir, settings.BASE_DIR)
            host_compose_dir = os.path.join(host_project_path, relative_compose_dir)

            compose_content = compose_content.replace('build: ./nvflare', f'build: {os.path.join(host_compose_dir, "nvflare")}')
            compose_content = compose_content.replace('./fl-client', os.path.join(host_compose_dir, 'fl-client'))
            compose_content = compose_content.replace('./server', os.path.join(host_compose_dir, 'server'))
            compose_content = compose_content.replace('./overseer', os.path.join(host_compose_dir, 'overseer'))

            with open(compose_file_path, 'w') as f:
                f.write(compose_content)
            
            logger.network.info("Modified compose file to use absolute host paths.")

        logger.network.info("Compose file found. Running docker compose build")
        # Ensure gunicorn & matching nvflare are installed in service image
        req_path = os.path.join(compose_dir, 'nvflare_compose', 'requirements.txt')
        try:
            os.makedirs(os.path.dirname(req_path), exist_ok=True)
            content = ''
            if os.path.exists(req_path):
                with open(req_path, 'r') as rf:
                    content = rf.read()
            lines = set(l.strip() for l in content.splitlines() if l.strip())
            changed = False
            if not any(l.startswith('nvflare') for l in lines):
                lines.add('nvflare==2.6.1')
                changed = True
            if 'gunicorn' not in lines:
                lines.add('gunicorn')
                changed = True
            if changed:
                with open(req_path, 'w') as wf:
                    wf.write('\n'.join(sorted(lines)) + '\n')
                logger.network.info('Updated nvflare_compose/requirements.txt with nvflare==2.6.1 and gunicorn')
        except Exception as e:
            logger.network.error(f'Failed to update nvflare_compose requirements: {e}')

        build_result = subprocess.run(['docker', 'compose', '-f', 'compose.yaml', 'build'], cwd=compose_dir, capture_output=True, text=True)
        logger.network.info(f"docker compose build stdout: {build_result.stdout}")
        logger.network.error(f"docker compose build stderr: {build_result.stderr}")

        logger.network.info("Running docker compose up -d")
        up_result = subprocess.run(['docker', 'compose', '-f', 'compose.yaml', 'up', '-d'], cwd=compose_dir, capture_output=True, text=True)
        logger.network.info(f"docker compose up stdout: {up_result.stdout}")
        logger.network.error(f"docker compose up stderr: {up_result.stderr}")

        # Connect the app container to the FLARE network
        try:
            net_name = os.path.basename(compose_dir) + "_default"  # e.g., 'prod_00_default'
            app_container_name = "mediswarmcloud"
            logger.network.info(f"Connecting app container {app_container_name} to network {net_name}")
            connect_result = subprocess.run(['docker', 'network', 'connect', net_name, app_container_name], capture_output=True, text=True)
            logger.network.info(f"docker network connect stdout: {connect_result.stdout}")
            logger.network.error(f"docker network connect stderr: {connect_result.stderr}")
        except Exception as e:
            logger.network.error(f"Failed to connect app container to FLARE network: {e}")

        swarm_network.status = 'RUNNING'
        swarm_network.save()

        # Run Preflight Check
        run_nvflare_preflight_check.delay(network_id, user_id)
    else:
        logger.network.error(f"Compose file not found at: {compose_file_path}")
        swarm_network.status = 'ERROR'
        swarm_network.save()


@shared_task
def run_nvflare_preflight_check(network_id, user_id):
    """
    Runs NVFlare preflight check using the admin startup kit.
    """
    try:
        swarm_network = SwarmNetwork.objects.get(identifier=network_id)
        project = swarm_network.project
        user = User.objects.get(id=user_id)
        logger = get_logger(user=user, project=project)
        
        project_name = project.title.replace(' ', '_')
        provision_dir = os.path.join(settings.BASE_DIR, 'workspaces', str(project.identifier), str(swarm_network.identifier))
        # NVFlare provision typically creates workspace/<project_name>/prod_00/
        admin_startup_dir = os.path.join(provision_dir, 'workspace', project_name, 'prod_00', 'admin@nvidia.com', 'startup')
        
        if not os.path.exists(admin_startup_dir):
            logger.network.error(f"Preflight check failed: Admin startup directory not found at {admin_startup_dir}")
            return

        logger.network.info("Starting NVFlare Preflight Check...")
        
        # We need to run this from the admin startup directory
        # The command is: python3 -m nvflare.tool.preflight_check -p .
        command = ['python3', '-m', 'nvflare.tool.preflight_check', '-p', admin_startup_dir]
        
        result = subprocess.run(command, capture_output=True, text=True)
        
        # Log the results
        if result.stdout:
            for line in result.stdout.splitlines():
                if line.strip():
                    LogEntry.objects.create(
                        user=user,
                        project=project,
                        swarm_network=swarm_network,
                        category=LogCategory.NETWORK,
                        source='preflight-check',
                        level='INFO',
                        message=line.strip()
                    )
        
        if result.stderr:
            for line in result.stderr.splitlines():
                if line.strip():
                    LogEntry.objects.create(
                        user=user,
                        project=project,
                        swarm_network=swarm_network,
                        category=LogCategory.NETWORK,
                        source='preflight-check',
                        level='ERROR',
                        message=line.strip()
                    )

        if result.returncode == 0:
            logger.network.info("NVFlare Preflight Check completed successfully.")
        else:
            logger.network.warning(f"NVFlare Preflight Check finished with issues (exit code {result.returncode}). Check logs for details.")

    except Exception as e:
        import traceback
        error_msg = f"Exception during NVFlare Preflight Check: {str(e)}\n{traceback.format_exc()}"
        print(error_msg)
        if 'logger' in locals():
            logger.network.error(error_msg)


@shared_task
def stop_swarm_network_task(network_id, user_id):
    swarm_network = SwarmNetwork.objects.get(identifier=network_id)
    user = User.objects.get(id=user_id)
    logger = get_logger(user=user, project=swarm_network.project)
    project_name = swarm_network.project.title.replace(' ', '_')
    provision_dir = os.path.join(settings.BASE_DIR, 'workspaces', str(swarm_network.project.identifier), str(swarm_network.identifier))
    compose_dir = os.path.join(provision_dir, 'workspace', project_name, 'prod_00')
    compose_file_path = os.path.join(compose_dir, 'compose.yaml')

    if os.path.exists(compose_file_path):
        host_project_path = os.getenv('HOST_PROJECT_PATH')
        if host_project_path:
            with open(compose_file_path, 'r') as f:
                compose_content = f.read()

            relative_compose_dir = os.path.relpath(compose_dir, settings.BASE_DIR)
            host_compose_dir = os.path.join(host_project_path, relative_compose_dir)

            compose_content = compose_content.replace('build: ./nvflare', f'build: {os.path.join(host_compose_dir, "nvflare")}')
            compose_content = compose_content.replace('./fl-client', os.path.join(host_compose_dir, 'fl-client'))
            compose_content = compose_content.replace('./server', os.path.join(host_compose_dir, 'server'))
            compose_content = compose_content.replace('./overseer', os.path.join(host_compose_dir, 'overseer'))

            with open(compose_file_path, 'w') as f:
                f.write(compose_content)

        logger.network.info(f"Stopping swarm network {swarm_network.name}")
        subprocess.run(['docker-compose', '-f', 'compose.yaml', 'down'], cwd=compose_dir)
        swarm_network.status = 'STOPPED'
        swarm_network.save()
        logger.network.info(f"Swarm network {swarm_network.name} stopped successfully")
    else:
        logger.network.error(f"Compose file not found at: {compose_file_path}")
        swarm_network.status = 'ERROR'
        swarm_network.save()