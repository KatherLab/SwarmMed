from celery import shared_task
import docker
from apps.logs.models import LogEntry, LogCategory, SwarmNetwork
from apps.project.models import Project
from apps.logs.logger import get_logger

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