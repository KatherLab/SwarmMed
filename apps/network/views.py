"""
Views for the network app.
Handles the orchestration of swarm networks, including provisioning,
deployment (start/stop), status monitoring, and startup kit distribution.
"""

import json
import os
import shutil
import zipfile

import yaml
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST
from logs import logger
from project.decorators import (
    project_context_required,
    project_membership_required,
)
from project.models import UserCurrentProject

from .models import SwarmNetwork, SwarmParticipant, UserCurrentNetwork
from .provision import generate_flare_startup_kit, is_valid_ip
from .tasks import start_swarm_network_task, stop_swarm_network_task
from .utils import (
    create_startup_kits_zip,
    get_hostname,
    get_tailscale_ip,
    is_tailscale_connected,
)


def get_user_project(request):
    """
    Retrieves the identifier of the project currently active for the user.

    Returns:
        tuple: (project_uuid_string, is_valid_boolean)
    """
    try:
        user_current_project = UserCurrentProject.objects.select_related(
            "project"
        ).get(user=request.user)
        if not user_current_project.project:
            return None, False

        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False


@login_required
@project_context_required
def network(request):
    """
    Main dashboard for managing swarm networks within the active project.
    Displays network statuses, connection info, and participant details.
    """
    current_project_uuid, _ = get_user_project(request)

    # Get the project object from the user's current project relation
    # Optimization: select_related to fetch project in one query
    current_project_relation = UserCurrentProject.objects.select_related(
        "project"
    ).get(user=request.user)
    project = current_project_relation.project

    # List all networks associated with this specific project
    # Optimization: select_related to fetch related fields in one query
    swarm_networks = SwarmNetwork.objects.filter(
        project=project
    ).select_related("project", "author")

    # Identify which network the user is currently focusing on
    try:
        # Optimization: select_related to fetch network and its project in one query
        current_network_rel = UserCurrentNetwork.objects.select_related(
            "network__project"
        ).get(user=request.user)
        current_network = current_network_rel.network
    except UserCurrentNetwork.DoesNotExist:
        current_network = None

    # If there is an active network, extract participant details from its project.yml
    # This helps display IP addresses and roles in the dashboard UI.
    participants_details = []
    if current_network:
        base_workspace = os.path.abspath("workspaces")
        project_yml_path = os.path.abspath(
            os.path.join(
                base_workspace,
                str(current_network.project.identifier),
                str(current_network.identifier),
                "project.yml",
            )
        )

        # Security check: Ensure path is within workspaces directory
        if project_yml_path.startswith(os.path.join(base_workspace, "")):
            if os.path.exists(project_yml_path):
                with open(project_yml_path) as f:
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
                                "ip": participant.get(
                                    "listening_host", "dynamic"
                                ),
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


@login_required
@project_context_required
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
        log = logger.get_logger(user=request.user, project=project)

        # Create the basic database record for this network
        swarm_network = SwarmNetwork.objects.create(
            name=network_name,
            project=project,
            description=description,
            author=request.user,
        )
        log.network.info(
            f"Initialized new network record: {network_name} (ID: {swarm_network.identifier})"
        )

        if creation_method == "create":
            # Extract client JSON data from the dynamic form fields
            clients_json = request.POST.getlist("clients")
            # Automatically detect the Tailscale IP for the server (this machine)
            # This allows remote clients (VPN) to connect to the server.
            server_ip = get_tailscale_ip()

            clients = []
            for c_json in clients_json:
                try:
                    c_data = json.loads(c_json)
                    # Sanitize client name immediately
                    safe_name = slugify(c_data.get("name", "client"))
                    if not safe_name:
                        continue
                    clients.append(
                        {"name": safe_name, "ip": c_data.get("ip", "")}
                    )
                except (json.JSONDecodeError, TypeError):
                    continue

            # Always include this creator node as a client participant.
            local_client_name = slugify(get_hostname() or "")
            local_client_ip = (
                server_ip if is_valid_ip(server_ip) else "host.docker.internal"
            )
            if local_client_name:
                clients.append(
                    {
                        "name": local_client_name,
                        "ip": local_client_ip,
                    }
                )

            # De-duplicate clients by participant name while preserving order.
            deduped_clients = []
            seen_client_names = set()
            for client in clients:
                participant_name = str(client.get("name", "")).strip()
                if not participant_name or participant_name in seen_client_names:
                    continue
                deduped_clients.append(client)
                seen_client_names.add(participant_name)
            clients = deduped_clients

            # Register participants in the database for tracking
            # NVFlare 2.7.1 single-server topology expects the canonical site name: "server"
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

            log.network.info(
                f"Provisioning network '{network_name}' with {len(clients)} clients.",
                clients=clients,
            )

            # Trigger background provisioning via NVFlare
            generate_flare_startup_kit(
                network_id=swarm_network.identifier,
                local_test=False,
                clients=clients,
                server_ip=server_ip,
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

                log.network.info(
                    f"User uploading startup kit for network '{network_name}'.",
                    uploaded_filename=startup_package.name,
                )

                # Extract the uploaded zip file into the project workspace securely
                with zipfile.ZipFile(startup_package, "r") as zip_ref:
                    # Get absolute path of the target directory for verification
                    abs_provision_dir = os.path.abspath(provision_dir)
                    for member in zip_ref.infolist():
                        # Determine the absolute target path for the member
                        # We use normpath and check if it's within the intended directory
                        member_path = os.path.normpath(member.filename)
                        if member_path.startswith(
                            "/"
                        ) or member_path.startswith(".."):
                            # Skip absolute paths or path traversal attempts in filename
                            continue

                        target_path = os.path.abspath(
                            os.path.join(abs_provision_dir, member_path)
                        )
                        # Ensure the target path is strictly within the intended directory
                        # We append a trailing slash to the prefix to prevent 'partial match'
                        # bypasses (e.g. /tmp/foo matching /tmp/foo-bar)
                        if not target_path.startswith(
                            os.path.join(abs_provision_dir, "")
                        ):
                            # Skip potentially malicious paths (Zip Slip)
                            continue
                        
                        # Special handling for requirements file: ensure it lands in the root provision_dir
                        if member_path == "docker_compose_requirements.txt":
                            with open(os.path.join(provision_dir, member_path), "wb") as f:
                                f.write(zip_ref.read(member))
                        else:
                            zip_ref.extract(member, provision_dir)

                # Normalize upload to runtime layout expected by start_swarm_network_task:
                # provision_dir/workspace/project_name/prod_00/
                project_name = slugify(project.title).replace("-", "_")
                prod_00_dir = os.path.join(
                    provision_dir, "workspace", project_name, "prod_00"
                )

                # If upload is a flat client zip, move its contents under prod_00.
                if os.path.exists(os.path.join(provision_dir, "startup")):
                    os.makedirs(prod_00_dir, exist_ok=True)
                    for item in os.listdir(provision_dir):
                        if item in {
                            "workspaces",
                            "docker_compose_requirements.txt",
                        }:
                            continue # Don't move the parent if recursive
                        src = os.path.join(provision_dir, item)
                        dst = os.path.join(prod_00_dir, item)
                        # Avoid moving the target dir into itself
                        if os.path.abspath(src) == os.path.abspath(os.path.join(provision_dir, "workspace")):
                            continue
                        shutil.move(src, dst)

                # Ensure admin_startup is properly nested for NVFlare API
                # Expected: session_dir/startup/fed_admin.json
                # We currently have prod_00_dir/admin_startup/fed_admin.json
                # We want prod_00_dir/admin_startup/startup/fed_admin.json
                admin_startup_dir = os.path.join(prod_00_dir, "admin_startup")
                if os.path.exists(admin_startup_dir):
                    # 1. Nest files into 'startup' subdirectory if not already done
                    nested_startup = os.path.join(admin_startup_dir, "startup")
                    if not os.path.exists(nested_startup):
                        os.makedirs(nested_startup, exist_ok=True)
                        for file in os.listdir(admin_startup_dir):
                            if file == "startup": continue
                            # Only move files, skip directories we might have just created
                            src_path = os.path.join(admin_startup_dir, file)
                            if os.path.isfile(src_path):
                                shutil.move(src_path, os.path.join(nested_startup, file))

                    # 2. Ensure 'local', 'transfer', and 'logs' directories exist.
                    # NVFlare API validates the presence of these folders in the workspace.
                    for folder in ["local", "transfer", "logs"]:
                        os.makedirs(os.path.join(admin_startup_dir, folder), exist_ok=True)

                    # 3. Inherit server_host.txt from sibling client kit if missing
                    admin_host_file = os.path.join(nested_startup, "server_host.txt")
                    if not os.path.exists(admin_host_file):
                        # Try sibling kit (prod_00/startup/server_host.txt)
                        sibling_host_file = os.path.join(prod_00_dir, "startup", "server_host.txt")
                        if os.path.exists(sibling_host_file):
                            shutil.copyfile(sibling_host_file, admin_host_file)

                    swarm_network.admin_startup_dir = os.path.abspath(
                        admin_startup_dir
                    )
                    swarm_network.save(update_fields=["admin_startup_dir"])

                swarm_network.status = "PROVISIONED"
                swarm_network.save()
                log.network.info(
                    f"Startup kit extracted and network '{network_name}' marked as PROVISIONED."
                )

        elif creation_method == "local_test":
            log.network.info(
                f"Provisioning local testing network '{network_name}'."
            )
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


@login_required
@project_membership_required
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


@login_required
@project_membership_required
def download_startup_kits(request, network_id):
    """
    Gathers the generated client startup kits (certs, config, start scripts),
    packages them into a ZIP file, and returns it as a download.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    log = logger.get_logger(user=request.user, project=swarm_network.project)

    log.network.info(
        f"User downloaded startup kits for network '{swarm_network.name}' (ID: {swarm_network.identifier})"
    )

    zip_buffer = create_startup_kits_zip(swarm_network)
    zip_content = zip_buffer.getvalue()

    # Check if the zip is empty (0 bytes) or just an empty container (22 bytes)
    if len(zip_content) <= 22:
        log.network.warning(
            f"Startup kit download failed for network '{swarm_network.name}': Empty zip file generated."
        )
        messages.error(
            request,
            "Startup kits not found. The network may not have been provisioned correctly.",
        )
        return redirect("network:network")

    response = HttpResponse(
        zip_content, content_type="application/zip"
    )
    filename = f"{swarm_network.name.replace(' ', '_')}_startup_kits.zip"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    return response


@login_required
@project_membership_required
def start_swarm_network(request, network_id):
    """
    Triggers the asynchronous Celery task to start the swarm network containers
    using containerized deployment.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    log = logger.get_logger(user=request.user, project=swarm_network.project)

    log.network.info(
        f"Starting swarm network '{swarm_network.name}' (ID: {swarm_network.identifier})."
    )

    swarm_network.status = "STARTING"
    swarm_network.save()

    # Dispatch the task to Celery
    start_swarm_network_task.delay(network_id, request.user.id)
    return redirect("network:network")


@login_required
@project_membership_required
def stop_swarm_network(request, network_id):
    """
    Triggers the asynchronous Celery task to stop and remove swarm
    network containers from containerized deployment.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    log = logger.get_logger(user=request.user, project=swarm_network.project)

    log.network.info(
        f"Stopping swarm network '{swarm_network.name}' (ID: {swarm_network.identifier})."
    )

    swarm_network.status = "STOPPING"
    swarm_network.save()

    # Dispatch the task to Celery
    stop_swarm_network_task.delay(network_id, request.user.id)
    return redirect("network:network")


@login_required
@project_membership_required
def get_swarm_network_status(request, network_id):
    """
    AJAX endpoint that returns the current status of a network.
    Used for live UI updates while provisioning or starting.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    return JsonResponse({"status": swarm_network.get_status_display()})


@require_POST
@login_required
@project_membership_required
def delete_swarm_network(request, network_id):
    """
    Deletes a swarm network configuration and its associated files from disk.
    Only the project author is permitted to delete networks.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    log = logger.get_logger(user=request.user, project=swarm_network.project)

    if swarm_network.project.author == request.user:
        # If the network is currently active, inform the user about the shutdown phase
        if swarm_network.status in ["RUNNING", "STARTING", "ERROR"]:
            messages.info(
                request,
                f"Network '{swarm_network.name}' is being stopped gracefully before deletion. This may take a moment.",
            )
        else:
            messages.success(
                request,
                f"Network '{swarm_network.name}' deleted successfully.",
            )

        log.network.warning(
            f"Deleting swarm network '{swarm_network.name}' (ID: {swarm_network.identifier})."
        )

        # The model's delete method handles Docker cleanup and file removal.
        # If the network is running, it will transition to STOPPING and delete asynchronously.
        swarm_network.delete()
    else:
        log.access.warning(
            f"Unauthorized delete attempt for network '{swarm_network.name}' by user {request.user.username}."
        )
        messages.error(
            request, "You do not have permission to delete this network."
        )

    return redirect("network:network")
