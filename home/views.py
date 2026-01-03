"""
View functions for the home application.
Handles the main dashboard display, aggregating statistics for projects,
storage, networking, and training progress.
"""

import json
import os
import re

from django.contrib.auth.decorators import login_required
from django.db import models
from django.shortcuts import render, redirect

from apps.data.utils import format_size, get_storage_stats
from apps.network.models import (
    SwarmNetwork,
    SwarmParticipant,
    UserCurrentNetwork
)
from apps.project.models import Project, UserCurrentProject
from apps.training.models import TrainingJob
from apps.logs.logger import get_logger

logger = get_logger()


def get_user_project_uuid(request):
    """
    Retrieves the unique identifier of the user's currently active project.

    Args:
        request: Django request object.

    Returns:
        tuple: (project_uuid as string, success_flag as boolean)
    """
    try:
        user_current_project = UserCurrentProject.objects.get(
            user=request.user)
        if not user_current_project.project:
            return None, False

        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False


@login_required
def index(request):
    """
    The main dashboard view.
    Aggregates data from various apps (Project, Data, Network, Training)
    to provide an overview of the user's activities.
    """
    user = request.user

    # 1. Project Aggregation
    # Fetch all projects where the user is either the author or a member.
    user_projects = Project.objects.filter(
        models.Q(author=user) | models.Q(members=user)
    ).distinct()

    # Identify the 'active' project for the UI.
    current_project = None
    try:
        current_project_relation = UserCurrentProject.objects.get(user=user)
        current_project = current_project_relation.project
    except UserCurrentProject.DoesNotExist:
        # Fallback: Use the most recently created project if no current project
        # is set.
        current_project = user_projects.order_by('-creation_date').first()

    # Calculate simple project counts for the dashboard cards.
    total_projects = user_projects.count()
    projects_as_author = user_projects.filter(author=user).count()
    projects_as_member = user_projects.filter(
        members=user).exclude(
        author=user).count()

    user_display_name = user.get_full_name() or user.username

    # 2. Storage Statistics (Current Active Project)
    total_files = 0
    total_storage = "0 B"
    total_folders = 0

    if current_project:
        # S3 storage is organized by project UUID.
        project_uuid = str(current_project.identifier)
        data_prefix = f"{project_uuid}/data/"

        try:
            # Query S3 (via our utility) for usage stats.
            total_size_bytes, folder_count, file_count = get_storage_stats(
                data_prefix)
            total_files = file_count
            total_storage = format_size(total_size_bytes)
            total_folders = folder_count
        except Exception as e:
            # Log errors and default to zero to avoid crashing the
            # dashboard.
            logger.project.warning(f"Error getting storage stats: {e}")

    # 3. Aggregated Storage Statistics (All Projects)
    total_files_all_projects = 0
    total_storage_all_projects = 0
    total_folders_all_projects = 0

    for project in user_projects:
        data_prefix = f"{str(project.identifier)}/data/"
        try:
            size_bytes, folder_count, file_count = get_storage_stats(
                data_prefix)
            total_files_all_projects += file_count
            total_storage_all_projects += size_bytes
            total_folders_all_projects += folder_count
        except Exception as e:
            logger.project.debug(f"Could not aggregate storage stats for project {project.identifier}: {e}")
            continue

    formatted_total_storage_all = format_size(total_storage_all_projects)

    # 4. Networking Statistics
    # Total networks available across all the user's projects.
    total_networks = SwarmNetwork.objects.filter(
        project__in=user_projects).count()

    current_network_obj = None
    network_partners = 0

    try:
        # Try to find the specific network the user has currently selected.
        user_current_network = UserCurrentNetwork.objects.get(
            user=request.user)
        if (user_current_network.network and
                user_current_network.network.project in user_projects):
            current_network_obj = user_current_network.network
    except UserCurrentNetwork.DoesNotExist:
        # Default to the most recently created network.
        current_network_obj = SwarmNetwork.objects.filter(
            project__in=user_projects
        ).order_by('-created_at').first()

    if current_network_obj:
        network_partners = SwarmParticipant.objects.filter(
            network=current_network_obj
        ).count()
        current_network_name = current_network_obj.name
    else:
        current_network_name = "No active network"

    # 5. Training Progress Tracking
    # This logic attempts to find the latest training job and calculate its real-time
    # progress by parsing logs on the filesystem.
    training_status = "Not started"
    training_progress = 0

    try:
        if current_network_obj:
            # Get the latest job for the active network.
            job = TrainingJob.objects.filter(
                network=current_network_obj
            ).order_by('-created_at').first()

            if job:
                training_status = job.status.title()

                # Fetch total rounds from the NVFlare server config.
                total_rounds = 0
                server_cfg_path = os.path.join(
                    'workspaces', str(job.project.identifier),
                    str(current_network_obj.identifier), 'job', 'app_server',
                    'config', 'config_fed_server.json'
                )

                if os.path.exists(server_cfg_path):
                    with open(server_cfg_path) as f:
                        cfg = json.load(f)
                        for workflow in cfg.get('workflows', []):
                            if workflow.get('id') == 'swarm_controller':
                                total_rounds = int(
                                    workflow.get(
                                        'args', {}).get(
                                        'num_rounds', 0))
                                break

                # Calculate completed rounds by scanning client log files.
                workspace_root = os.path.join(
                    'workspaces', str(job.project.identifier),
                    str(current_network_obj.identifier), 'workspace'
                )
                rounds_finished = 0
                ended = False

                # Check logs for both default clients.
                for client in ('fl-client-1', 'fl-client-2'):
                    base_client = None
                    for root, dirs, files in os.walk(workspace_root):
                        if os.path.basename(root) == client:
                            base_client = root
                            break

                    if not base_client or not os.path.isdir(base_client):
                        continue

                    # Find the latest 'run' directory in the client workspace.
                    runs = [
                        d for d in os.listdir(base_client)
                        if os.path.isdir(os.path.join(base_client, d))
                    ]
                    if not runs:
                        continue

                    runs.sort(
                        key=lambda d: os.path.getmtime(
                            os.path.join(
                                base_client,
                                d)),
                        reverse=True)
                    client_dir = os.path.join(base_client, runs[0])

                    for fname in ('log_fl.txt', 'log.txt'):
                        fpath = os.path.join(client_dir, fname)
                        if os.path.exists(fpath):
                            try:
                                with open(fpath, 'r') as lf:
                                    data = lf.read()
                                    # Use regex to find the highest 'finished
                                    # round' number.
                                    for match in re.finditer(
                                        r'finished training round (\d+)', data
                                    ):
                                        round_num = int(match.group(1))
                                        if round_num > rounds_finished:
                                            rounds_finished = round_num

                                    # Check for completion markers.
                                    if ('ending workflow swarm_controller' in data or
                                            'finished with RC 0' in data):
                                        ended = True
                            except OSError:
                                pass

                if total_rounds > 0:
                    training_progress = min(
                        100, int(rounds_finished * 100 / total_rounds)
                    )

                if ended or training_progress >= 100:
                    training_progress = 100
                    training_status = 'Completed'
                elif job.status == 'RUNNING':
                    training_status = 'Running'
    except Exception as e:
        # Training tracking is secondary; if it fails, we just show "Not
        # started" and log the error for debugging.
        logger.project.debug(f"Error tracking training progress on dashboard: {e}")

    # 6. Assemble the Context for the template.
    context = {
        'segment': 'dashboard',
        'user_display_name': user_display_name,
        'current_project': current_project,
        'total_projects': total_projects,
        'projects_as_author': projects_as_author,
        'projects_as_member': projects_as_member,

        # Storage usage for the active project.
        'total_files': total_files,
        'total_storage': total_storage,
        'total_folders': total_folders,

        # Overall storage usage.
        'total_files_all_projects': total_files_all_projects,
        'total_storage_all_projects': formatted_total_storage_all,
        'total_folders_all_projects': total_folders_all_projects,

        # Network details.
        'total_networks': total_networks,
        'current_network': current_network_name,
        'current_network_status': (
            current_network_obj.get_status_display()
            if current_network_obj else None
        ),
        'network_partners': network_partners,

        # Real-time training metrics.
        'training_status': training_status,
        'training_progress': training_progress,
    }

    return render(request, "dashboard/index.html", context)


def starter(request):
    """Simple starter page view."""
    return render(request, "pages/starter.html", {})
