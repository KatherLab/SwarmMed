"""
Views for the logs app.
Provides the main logs dashboard and functionality to download
historical logs and real-time container logs.
"""

import os
import subprocess  # nosec B404
import shutil
from datetime import timedelta

import yaml
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.http import HttpResponse

from apps.users.decorators import developer_required
from .models import LogEntry, LogCategory
from apps.logs.logger import get_logger

logger = get_logger()

# Attempt to import current project tracking
try:
    from ..project.models import UserCurrentProject
except ImportError:
    UserCurrentProject = None


def get_user_project(request):
    """
    Helper function to retrieve the currently active project for the user.
    """
    if not UserCurrentProject:
        return None, False

    try:
        user_current_project = UserCurrentProject.objects.get(
            user=request.user)
        if not user_current_project.project:
            return None, False
        return user_current_project.project, True
    except UserCurrentProject.DoesNotExist:
        return None, False


@developer_required
@login_required(login_url='/users/signin/')
def download_log_category(request, category_key):
    """
    Generates and returns a plain-text file containing all historical
    logs for a specific category within the active project.
    """
    project, is_valid = get_user_project(request)
    if not is_valid:
        return HttpResponse("No project selected.", status=404)

    # Validate that the requested category exists
    valid_categories = [choice[0] for choice in LogCategory.choices]
    if category_key not in valid_categories:
        return HttpResponse("Invalid category.", status=404)

    # Retrieve all matching log entries, ordered by time
    log_entries = LogEntry.objects.filter(
        project=project,
        category=category_key
    ).select_related('user').order_by('timestamp')

    # Build the text content for the log file
    log_lines = []
    for entry in log_entries:
        timestamp_str = entry.timestamp.strftime('%Y-%m-%d %H:%M:%S')
        line = (
            f"[{timestamp_str}][{entry.user.email}] {entry.level} - "
            f"[{entry.source}] {entry.message}"
        )
        log_lines.append(line)

    log_content = "\n".join(log_lines)

    # Create the HTTP response with appropriate headers for a file download
    response = HttpResponse(log_content, content_type='text/plain')
    filename = f"{project.title.replace(' ', '_')}_{category_key}_logs.txt"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    return response


@developer_required
@login_required(login_url='/users/signin/')
def logs_dashboard(request):
    """
    The main logs dashboard view.
    It aggregates logs from the database and, for training logs,
    attempts to fetch real-time logs directly from Docker containers.
    """
    project, is_valid = get_user_project(request)
    if not is_valid:
        return render(
            request,
            "apps/logs/no_project_selected.html",
            {"segment": "logs"}
        )

    categories_list = []

    # Iterate through each defined log category to build the dashboard sections
    for category_choice in LogCategory.choices:
        category_key = category_choice[0]
        category_display = category_choice[1]

        # 1. Fetch recent logs (last 50) from the database
        recent_logs = list(LogEntry.objects.filter(
            project=project,
            category=category_key
        ).select_related('user').order_by('-timestamp')[:50])

        # 2. Special handling for Training logs: Fetch live Docker logs
        if category_key == 'training':
            try:
                from apps.network.models import UserCurrentNetwork

                # Check if the user has a currently active network
                user_network = UserCurrentNetwork.objects.get(
                    user=request.user
                ).network

                if user_network:
                    # Construct path to the docker-compose file for this
                    # network
                    project_name = project.title.replace(' ', '_')
                    compose_path = os.path.join(
                        'workspaces',
                        str(project.identifier),
                        str(user_network.identifier),
                        'workspace',
                        project_name,
                        'prod_00',
                        'compose.yaml'
                    )

                    if os.path.exists(compose_path):
                        with open(compose_path, 'r') as f:
                            compose_data = yaml.safe_load(f)

                        # Identify all services defined in the compose file
                        if compose_data and 'services' in compose_data:
                            for service_name in compose_data['services']:
                                # Determine the container name
                                container_name = compose_data['services'][service_name].get(
                                    'container_name', service_name)

                                # Execute 'docker logs' to get live output
                                docker_path = shutil.which('docker') or 'docker'
                                try:
                                    result = subprocess.run(  # nosec B603
                                        [docker_path, 'logs', '--tail', '100', container_name],
                                        capture_output=True,
                                        text=True,
                                        check=False
                                    )
                                    log_output = result.stdout or result.stderr

                                    # Convert raw output lines into mock
                                    # objects for the template
                                    for line in log_output.splitlines():
                                        mock_entry = {
                                            'message': line,
                                            'source': container_name,
                                            'timestamp': timezone.now(),
                                            'level': 'INFO',
                                            'user': request.user
                                        }
                                        # Simple class to mimic a Django model
                                        # object

                                        class LogMock:
                                            def __init__(self, **kwargs):
                                                self.__dict__.update(kwargs)

                                        recent_logs.insert(
                                            0, LogMock(**mock_entry))
                                except Exception as e:
                                    # Log if Docker command fails
                                    logger.project.warning(f"Failed to fetch live logs for {container_name}: {e}")
            except (UserCurrentNetwork.DoesNotExist, FileNotFoundError) as e:
                logger.project.debug(f"Could not fetch live logs: {e}")

        # 3. Calculate statistics for the UI
        total_count = LogEntry.objects.filter(
            project=project,
            category=category_key
        ).count()

        # Count entries in the last 24 hours
        yesterday = timezone.now() - timedelta(days=1)
        recent_count = LogEntry.objects.filter(
            project=project,
            category=category_key,
            timestamp__gte=yesterday
        ).count()

        categories_list.append({
            'key': category_key,
            'display_name': category_display,
            'entries': recent_logs,
            'total_entries': total_count,
            'recent_entries': recent_count,
        })

    context = {
        'segment': 'logs',
        'project': project,
        'categories_list': categories_list,
    }
    return render(request, "apps/logs/logs.html", context)


# Alias to match URL configuration
logs = logs_dashboard
