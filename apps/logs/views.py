"""
Views for the logs app.
Provides the main logs dashboard and functionality to download
historical logs and real-time container logs.
"""

import os
import shutil

# Bandit B404: subprocess is required for optional docker log streaming; no shell=True usage.
import subprocess  # nosec B404
from datetime import timedelta

import yaml
from common.utils import get_safe_slug
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from project.decorators import project_context_required
from users.decorators import developer_required

from logs.logger import get_logger

from .models import LogCategory, LogEntry

logger = get_logger()

# Attempt to import current project tracking
try:
    from project.models import UserCurrentProject
except ImportError:
    UserCurrentProject = None


def get_user_project(request):
    """
    Helper function to retrieve the currently active project for the user.
    """
    if not UserCurrentProject:
        return None, False

    try:
        user_current_project = UserCurrentProject.objects.select_related(
            "project"
        ).get(user=request.user)
        if not user_current_project.project:
            return None, False
        return user_current_project.project, True
    except UserCurrentProject.DoesNotExist:
        return None, False


@developer_required
@login_required
@project_context_required
def download_log_category(request, category_key):
    """
    Generates and returns a plain-text file containing all historical
    logs for a specific category within the active project.
    """
    project, _ = get_user_project(request)

    # Validate that the requested category exists
    valid_categories = [choice[0] for choice in LogCategory.choices]
    if category_key not in valid_categories:
        return HttpResponse("Invalid category.", status=404)

    # Retrieve matching log entries
    # NOTE: EncryptedTextField doesn't support DB filtering, so we filter in memory
    log_entries_all = (
        LogEntry.objects.filter(project=project, category=category_key)
        .select_related("user")
        .order_by("timestamp")
    )

    # Build the text content for the log file
    log_lines = []
    for entry in log_entries_all:
        if entry.message.startswith("Audit:"):
            continue

        timestamp_str = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"[{timestamp_str}][{entry.user.email if entry.user else 'System'}] {entry.level} - "
            f"{entry.message}"
        )
        log_lines.append(line)

    log_content = "\n".join(log_lines)

    # Create the HTTP response with appropriate headers for a file download
    response = HttpResponse(log_content, content_type="text/plain")
    project_identifier = project.identifier if project else "project"
    project_slug = get_safe_slug(
        getattr(project, "title", ""), project_identifier
    )
    filename = f"{project_slug.replace('-', '_')}_{category_key}_logs.txt"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    return response


@developer_required
@login_required
@project_context_required
def logs_dashboard(request):
    """
    The main logs dashboard view.
    It aggregates logs from the database and, for training logs,
    attempts to fetch real-time logs directly from Docker containers.
    """
    project, _ = get_user_project(request)

    categories_list = []

    # Iterate through each defined log category to build the dashboard sections
    for category_choice in LogCategory.choices:
        category_key = category_choice[0]
        category_display = category_choice[1]

        # 1. Fetch recent logs from the database
        # NOTE: EncryptedTextField doesn't support DB filtering, so we filter in memory
        # Fetching a larger batch to account for filtered entries
        db_logs = list(
            LogEntry.objects.filter(project=project, category=category_key)
            .select_related("user")
            .order_by("-timestamp")[:200]
        )

        recent_logs = []
        for entry in db_logs:
            if not entry.message.startswith("Audit:"):
                recent_logs.append(entry)
            if len(recent_logs) >= 50:
                break

        # 2. Special handling for Training logs: Fetch live Docker logs
        live_log_count = 0
        if category_key == "training":
            try:
                from network.models import SwarmNetwork

                # Use resolve_current to find the running network for the project
                user_network = SwarmNetwork.resolve_current(request.user)

                if user_network:
                    # Construct path to the docker-compose file for this
                    # network
                    project_name = get_safe_slug(
                        project.title, project.identifier
                    ).replace("-", "_")
                    compose_path = os.path.join(
                        "workspaces",
                        str(project.identifier),
                        str(user_network.identifier),
                        "workspace",
                        project_name,
                        "prod_00",
                        "compose.yaml",
                    )

                    if os.path.exists(compose_path):
                        with open(compose_path) as f:
                            compose_data = yaml.safe_load(f)

                        # Identify all services defined in the compose file
                        if compose_data and "services" in compose_data:
                            for service_name in compose_data["services"]:
                                # Determine the container name
                                container_name = compose_data["services"][
                                    service_name
                                ].get("container_name", service_name)

                                # Execute 'docker logs' to get live output
                                docker_path = (
                                    shutil.which("docker") or "docker"
                                )
                                try:
                                    # Bandit B603: args are a fixed list; shell=False; binary resolved via shutil.which.
                                    result = subprocess.run(  # nosec B603
                                        [
                                            docker_path,
                                            "logs",
                                            "--tail",
                                            "100",
                                            container_name,
                                        ],
                                        capture_output=True,
                                        text=True,
                                        check=False,
                                    )
                                    if result.returncode != 0:
                                        stderr_text = (result.stderr or "").strip()
                                        if "No such container" in stderr_text:
                                            continue

                                    log_output = result.stdout or result.stderr

                                    # Convert raw output lines into mock
                                    # objects for the template
                                    for line in log_output.splitlines():
                                        # Filter out Audit logs from live stream if applicable
                                        if line.startswith("Audit:"):
                                            continue

                                        live_log_count += 1
                                        mock_entry = {
                                            "id": uuid.uuid4(),
                                            "message": line,
                                            "source": container_name,
                                            "timestamp": timezone.now(),
                                            "level": "INFO",
                                            "user": request.user,
                                        }
                                        # Simple class to mimic a Django model
                                        # object

                                        class LogMock:
                                            def __init__(self, **kwargs):
                                                self.__dict__.update(kwargs)

                                        recent_logs.insert(
                                            0, LogMock(**mock_entry)
                                        )
                                except Exception as e:
                                    # Log if Docker command fails
                                    logger.project.warning(
                                        f"Failed to fetch live logs for {container_name}: {e}"
                                    )
            except (UserCurrentNetwork.DoesNotExist, FileNotFoundError) as e:
                logger.project.debug(f"Could not fetch live logs: {e}")

        # 3. Calculate statistics for the UI
        # NOTE: Since we can't filter encrypted fields in DB, we have to fetch and filter.
        # For performance on large logs, this might need an 'is_audit' boolean field in the future.
        all_category_logs = LogEntry.objects.filter(
            project=project, category=category_key
        )

        # Calculate in-memory for accuracy due to encryption
        total_count = live_log_count
        recent_count = live_log_count
        yesterday = timezone.now() - timedelta(days=1)

        for entry in all_category_logs:
            if not entry.message.startswith("Audit:"):
                total_count += 1
                if entry.timestamp >= yesterday:
                    recent_count += 1

        categories_list.append(
            {
                "key": category_key,
                "display_name": category_display,
                "entries": recent_logs,
                "total_entries": total_count,
                "recent_entries": recent_count,
            }
        )

    context = {
        "segment": "logs",
        "project": project,
        "categories_list": categories_list,
    }
    return render(request, "apps/logs/logs.html", context)


@developer_required
@login_required
@project_context_required
def load_more_logs(request, category_key):
    """
    AJAX view to fetch older logs for infinite scrolling.
    """
    project, _ = get_user_project(request)
    last_timestamp_str = request.GET.get("last_timestamp")

    query = LogEntry.objects.filter(project=project, category=category_key)

    if last_timestamp_str:
        last_timestamp = None
        try:
            last_timestamp = parse_datetime(last_timestamp_str)
        except (TypeError, ValueError, OverflowError):
            last_timestamp = None
        if last_timestamp:
            query = query.filter(timestamp__lt=last_timestamp)

    # Fetch a batch and filter in memory
    db_logs = list(query.select_related("user").order_by("-timestamp")[:100])

    log_data = []
    for entry in db_logs:
        if entry.message.startswith("Audit:"):
            continue

        log_data.append(
            {
                "timestamp": entry.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "raw_timestamp": entry.timestamp.isoformat(),
                "user": entry.user.email if entry.user else "System",
                "level": entry.level,
                "message": entry.message,
            }
        )
        if len(log_data) >= 50:
            break

    return JsonResponse({"logs": log_data})


# Alias to match URL configuration
logs = logs_dashboard
