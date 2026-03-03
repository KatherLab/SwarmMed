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
from .provision import generate_flare_startup_kit
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
            # This allows remote clients (VPN) to connect to the overseer/server.
            server_ip = get_tailscale_ip()

            clients = []
            for c_json in clients_json:
                try:
                    c_data = json.loads(c_json)
                    # Sanitize client name immediately
                    safe_name = slugify(c_data.get("name", "client"))
                    clients.append(
                        {"name": safe_name, "ip": c_data.get("ip", "")}
                    )
                except (json.JSONDecodeError, TypeError):
                    continue

            # Register participants in the database for tracking
            server_count = max(1, len(clients))
            for server_index in range(server_count):
                SwarmParticipant.objects.create(
                    network=swarm_network,
                    user=request.user,
                    role="SERVER",
                    participant_id=f"server{server_index + 1}",
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
                        zip_ref.extract(member, provision_dir)

                # Post-Extraction: Check for compose.yaml and generate if missing (Client Node case)
                # Client zips only contain: startup/, local/, transfer/, etc.
                # They lack the 'workspace/project/prod_00' structure and 'compose.yaml'.
                
                # We need to construct the expected path for start_swarm_network_task:
                # provision_dir/workspace/project_name/prod_00/compose.yaml
                
                project_name = slugify(project.title).replace("-", "_")
                prod_00_dir = os.path.join(
                    provision_dir, "workspace", project_name, "prod_00"
                )
                
                # If the upload was a flat client zip, the files are in provision_dir root or subfolder.
                # We need to move them to the expected structure or adjust the structure.
                # Heuristic: Check if 'startup' folder exists in provision_dir
                if os.path.exists(os.path.join(provision_dir, "startup")):
                    # This is a client zip extracted to root. Move to prod_00.
                    os.makedirs(prod_00_dir, exist_ok=True)
                    for item in os.listdir(provision_dir):
                        if item == "workspaces": continue # Don't move the parent if recursive
                        src = os.path.join(provision_dir, item)
                        dst = os.path.join(prod_00_dir, item)
                        # Avoid moving the target dir into itself
                        if os.path.abspath(src) == os.path.abspath(os.path.join(provision_dir, "workspace")):
                            continue
                        shutil.move(src, dst)
                        
                compose_path = os.path.join(prod_00_dir, "compose.yaml")

                admin_startup_dir = os.path.join(prod_00_dir, "admin_startup")
                if os.path.exists(admin_startup_dir):
                    swarm_network.admin_startup_dir = os.path.abspath(
                        admin_startup_dir
                    )
                    swarm_network.save(update_fields=["admin_startup_dir"])
                
                if not os.path.exists(compose_path):
                    log.network.info("Compose file missing in upload. Generating client compose file.")
                    # Detect participant name from fed_client.json or similar
                    participant_id = "client" # Fallback
                    startup_dir = os.path.join(prod_00_dir, "startup")
                    
                    # Ensure startup scripts are executable
                    for script_name in ["start.sh", "sub_start.sh", "stop_fl.sh"]:
                        script_path = os.path.join(startup_dir, script_name)
                        if os.path.exists(script_path):
                            os.chmod(script_path, 0o755)

                    if os.path.exists(os.path.join(startup_dir, "fed_client.json")):
                        # It's a client
                        try:
                            with open(os.path.join(startup_dir, "fed_client.json")) as f:
                                conf = json.load(f)
                                # Try to find name in config (usually hidden in uid or similar, but often filename is better)
                                # Defaulting to 'fl_client' service name
                                pass
                        except Exception:
                            pass
                        
                        # Generate simple compose.yaml for client
                        # We use the same image as the project (python:3.10-slim + requirements)
                        # But simpler: just run the start.sh
                        
                        client_compose_content = {
                            "services": {
                                "fl_client": {
                                    "image": "python:3.10-slim", # Should match provision.py builder or custom image
                                    "volumes": [
                                        # Mount the prod_00 directory to /workspace
                                        f"./:{'/workspace'}"
                                    ],
                                    "working_dir": "/workspace/startup",
                                    # Keep container alive by tailing /dev/null, as start.sh runs in background
                                    "command": '/bin/bash -c "./start.sh && tail -f /dev/null"',
                                    "restart": "always",
                                    "network_mode": "host" # Simplifies communication for clients
                                }
                            }
                        }
                        
                        # We need to install requirements first? 
                        # The start.sh usually assumes environment is ready.
                        # Ideally we should use the same builder logic as provision.py
                        # For now, we assume the user will have a proper environment or we use a standard image.
                        # NVFlare docker image is better: nvflare/nvflare
                        
                        client_compose_content["services"]["fl_client"]["image"] = "nvflare/nvflare:2.4.1"
                        
                        # Extract Client Name and Server IP
                        server_ip = os.environ.get("SWARMCLOUD_OVERSEER_HOST", "").strip()
                        try:
                            overseer_agent_args = conf.get("overseer_agent", {}).get("args", {})
                            client_name = overseer_agent_args.get("name", "client")
                            overseer_url = overseer_agent_args.get("overseer_end_point", "")

                            if not server_ip:
                                host_file = os.path.join(startup_dir, "overseer_host.txt")
                                if os.path.exists(host_file):
                                    try:
                                        with open(host_file, "r") as hf:
                                            server_ip = hf.read().strip()
                                    except Exception:
                                        server_ip = ""
                            
                            # Use direct python command to avoid start.sh zombie issues and capture logs
                            # We hardcode org=nvidia as per provision.py
                            # Wrap in sh -c to capture output to file AND stdout
                            python_cmd = (
                                f"python3 -u -m nvflare.private.fed.app.client.client_train "
                                f"-m /workspace -s fed_client.json "
                                f"--set secure_train=true uid={client_name} org=nvidia config_folder=config"
                            )
                            
                            client_compose_content["services"]["fl_client"]["command"] = [
                                "/bin/sh", 
                                "-c", 
                                f"echo 'Starting Client: {client_name}' > /workspace/docker_startup_log.txt && "
                                f"echo 'Server IP: {server_ip or 'unknown'}' >> /workspace/docker_startup_log.txt && "
                                f"{python_cmd} 2>&1 | tee -a /workspace/docker_startup_log.txt"
                            ]
                            
                            # Add PYTHONPATH as per sub_start.sh
                            client_compose_content["services"]["fl_client"]["environment"] = {
                                "PYTHONPATH": "/local/custom"
                            }
                            # Emulate TTY to match manual run behavior
                            client_compose_content["services"]["fl_client"]["tty"] = True
                            client_compose_content["services"]["fl_client"]["stdin_open"] = True

                            if not server_ip and overseer_url:
                                # Parse IP from https://IP:PORT or http://IP:PORT
                                parsed_host = overseer_url.split("://")[-1].split(":")[0]
                                if parsed_host and parsed_host != "overseer":
                                    server_ip = parsed_host

                            if server_ip:
                                extra_hosts = {
                                    f"server:{server_ip}",
                                    f"overseer:{server_ip}",
                                }
                                aliases_file = os.path.join(
                                    startup_dir, "server_aliases.txt"
                                )
                                if os.path.exists(aliases_file):
                                    try:
                                        with open(aliases_file, "r") as sf:
                                            for alias in sf.read().splitlines():
                                                alias = alias.strip()
                                                if alias:
                                                    extra_hosts.add(
                                                        f"{alias}:{server_ip}"
                                                    )
                                    except Exception:
                                        pass

                                client_compose_content["services"]["fl_client"]["extra_hosts"] = list(extra_hosts)
                        except Exception as e:
                            log.network.warning(f"Error configuring client compose: {e}")

                        with open(compose_path, "w") as f:
                            yaml.dump(client_compose_content, f)
                            
                    elif os.path.exists(os.path.join(startup_dir, "fed_server.json")):
                         # It's a server (if they uploaded a server kit manually)
                         pass

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
    using docker-compose.
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
    network containers.
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
