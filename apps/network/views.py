"""
Views for the network app.
Handles the orchestration of swarm networks, including provisioning,
deployment (start/stop), status monitoring, and startup kit distribution.
"""

import json
import os
import zipfile

import yaml
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import HttpResponse, JsonResponse

from apps.project.models import UserCurrentProject
from .models import SwarmNetwork, SwarmParticipant, UserCurrentNetwork
from .provision import generate_flare_startup_kit
from .tasks import start_swarm_network_task, stop_swarm_network_task
from .utils import (
    get_tailscale_ip,
    is_tailscale_connected,
    get_hostname,
    create_startup_kits_zip,
)


def get_user_project(request):
    """
    Retrieves the identifier of the project currently active for the user.

    Returns:
        tuple: (project_uuid_string, is_valid_boolean)
    """
    try:
        user_current_project = UserCurrentProject.objects.get(
            user=request.user
        )
        if not user_current_project.project:
            return None, False

        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False


@login_required(login_url="/users/signin/")
def network(request):
    """
    Main dashboard for managing swarm networks within the active project.
    Displays network statuses, connection info, and participant details.
    """
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return render(
            request,
            "apps/network/no_project_selected.html",
            {"segment": "network"},
        )

    # Get the project object from the user's current project relation
    current_project_relation = UserCurrentProject.objects.get(
        user=request.user
    )
    project = current_project_relation.project

    # List all networks associated with this specific project
    swarm_networks = SwarmNetwork.objects.filter(project=project)

    # Identify which network the user is currently focusing on
    try:
        current_network = UserCurrentNetwork.objects.get(
            user=request.user
        ).network
    except UserCurrentNetwork.DoesNotExist:
        current_network = None

    # If there is an active network, extract participant details from its project.yml
    # This helps display IP addresses and roles in the dashboard UI.
    participants_details = []
    if current_network:
        base_workspace = os.path.abspath("workspaces")
        project_yml_path = os.path.abspath(os.path.join(
            base_workspace,
            str(current_network.project.identifier),
            str(current_network.identifier),
            "project.yml",
        ))
        
        # Security check: Ensure path is within workspaces directory
        if project_yml_path.startswith(os.path.join(base_workspace, "")):
             if os.path.exists(project_yml_path):
                with open(project_yml_path, "r") as f:
                    project_yml = yaml.safe_load(f)
                    # Parse the list of participants defined in NVFlare lighter
                    # config
                    for participant in project_yml.get("participants", []):
                        participants_details.append(
                            {
                                "name": participant.get("name"),
                                "org": participant.get("org"),
                                # NVFlare uses 'listening_host' for static IP
                                # assignments
                                "ip": participant.get("listening_host", "dynamic"),
                            }
                        )

    context = {
        "segment": "network",
        "project": project,
        "tailscale_status": is_tailscale_connected(),
        "tailscale_ip": get_tailscale_ip(),
        "hostname": get_hostname(),
        "swarm_networks": swarm_networks,
        "current_network": current_network,
        "participants_details": participants_details,
    }
    return render(request, "apps/network/network.html", context)


@login_required(login_url="/users/signin/")
def new_network(request):
    """
    Handles the creation of a new swarm network configuration.

    Supports three methods:
    1. 'create': Provision a new network from a list of client names and IPs.
    2. 'upload': Import an existing FLARE startup kit zip file.
    3. 'local_test': Auto-generate a local-only testing network.
    """
    if request.method == "POST":
        creation_method = request.POST.get("creation_method")
        network_name = request.POST.get("title")
        description = request.POST.get("description")

        current_project_relation = UserCurrentProject.objects.get(
            user=request.user
        )
        project = current_project_relation.project

        # Create the basic database record for this network
        swarm_network = SwarmNetwork.objects.create(
            name=network_name,
            project=project,
            description=description,
            author=request.user,
        )

        if creation_method == "create":
            # Extract client JSON data from the dynamic form fields
            clients_json = request.POST.getlist("clients")
            clients = [json.loads(c) for c in clients_json]

            # Register participants in the database for tracking
            SwarmParticipant.objects.create(
                network=swarm_network,
                user=request.user,
                role="SERVER",
                participant_id="server",
            )
            for client_data in clients:
                SwarmParticipant.objects.create(
                    network=swarm_network,
                    user=request.user,
                    role="CLIENT",
                    participant_id=client_data["name"],
                )

            # Trigger background provisioning via NVFlare
            generate_flare_startup_kit(
                network_id=swarm_network.identifier,
                local_test=False,
                clients=clients,
            )

        elif creation_method == "upload":
            # Handle user upload of a pre-existing startup kit
            startup_package = request.FILES.get("startup_package")
            if startup_package:
                provision_dir = os.path.join(
                    "workspaces",
                    str(project.identifier),
                    str(swarm_network.identifier),
                )
                os.makedirs(provision_dir, exist_ok=True)

                # Extract the uploaded zip file into the project workspace securely
                with zipfile.ZipFile(startup_package, "r") as zip_ref:
                    # Get absolute path of the target directory for verification
                    abs_provision_dir = os.path.abspath(provision_dir)
                    for member in zip_ref.infolist():
                        # Determine the absolute target path for the member
                        target_path = os.path.abspath(
                            os.path.join(abs_provision_dir, member.filename)
                        )
                        # Ensure the target path is within the intended directory
                        if not target_path.startswith(abs_provision_dir):
                            # Skip potentially malicious paths (Zip Slip)
                            continue
                        zip_ref.extract(member, provision_dir)

                swarm_network.status = "PROVISIONED"
                swarm_network.save()

        elif creation_method == "local_test":
            # Generate a network intended for development/testing on a single
            # machine
            generate_flare_startup_kit(
                network_id=swarm_network.identifier,
                local_test=True,
                clients=[],
            )

        return redirect("network:network")

    context = {
        "segment": "network",
    }
    return render(request, "apps/network/new_network.html", context)


@login_required(login_url="/users/signin/")
def set_current_network(request, network_id):
    """
    Sets the specified network as the 'active' network for the current user.
    """
    network_obj = get_object_or_404(SwarmNetwork, identifier=network_id)
    current_project_relation = UserCurrentProject.objects.get(
        user=request.user
    )

    # Security check: Ensure the network belongs to the user's active project
    if network_obj.project == current_project_relation.project:
        current_network, _ = UserCurrentNetwork.objects.get_or_create(
            user=request.user
        )
        current_network.network = network_obj
        current_network.save()

    return redirect("network:network")


@login_required(login_url="/users/signin/")
def download_startup_kits(request, network_id):
    """
    Gathers the generated client startup kits (certs, config, start scripts),
    packages them into a ZIP file, and returns it as a download.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    zip_buffer = create_startup_kits_zip(swarm_network)

    response = HttpResponse(
        zip_buffer.getvalue(), content_type="application/zip"
    )
    filename = f"{swarm_network.name.replace(' ', '_')}_startup_kits.zip"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    return response


@login_required(login_url="/users/signin/")
def start_swarm_network(request, network_id):
    """
    Triggers the asynchronous Celery task to start the swarm network containers
    using docker-compose.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    swarm_network.status = "STARTING"
    swarm_network.save()

    # Dispatch the task to Celery
    start_swarm_network_task.delay(network_id, request.user.id)
    return redirect("network:network")


@login_required(login_url="/users/signin/")
def stop_swarm_network(request, network_id):
    """
    Triggers the asynchronous Celery task to stop and remove swarm
    network containers.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    swarm_network.status = "STOPPING"
    swarm_network.save()

    # Dispatch the task to Celery
    stop_swarm_network_task.delay(network_id, request.user.id)
    return redirect("network:network")


@login_required(login_url="/users/signin/")
def get_swarm_network_status(request, network_id):
    """
    AJAX endpoint that returns the current status of a network.
    Used for live UI updates while provisioning or starting.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    return JsonResponse({"status": swarm_network.get_status_display()})


@require_POST
@login_required(login_url="/users/signin/")
def delete_swarm_network(request, network_id):
    """
    Deletes a swarm network configuration and its associated files from disk.
    Only the project author is permitted to delete networks.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    if swarm_network.project.author == request.user:
        # The model's delete method handles Docker cleanup and file removal
        swarm_network.delete()
    return redirect("network:network")
