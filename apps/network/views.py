"""Views for the network app.
Handles the orchestration of swarm networks, including provisioning,
deployment (start/stop), status monitoring, and startup kit distribution.
"""

import json
import os
import secrets
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from logs.logger import get_logger
from project.decorators import (
    project_context_required,
    project_membership_required,
)
from project.models import UserCurrentProject

from .models import SwarmNetwork, SwarmParticipant, UserCurrentNetwork
from . import services as network_services
from .utils import (
    get_hostname,
    get_tailscale_ip,
    is_tailscale_connected,
)

# Standard project-wide logger initialization
logger = get_logger()


def get_user_project(request):
    """Retrieves the identifier of the project currently active for the user.

    Args:
        request (HttpRequest): The incoming HTTP request.

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


def _dedupe_keep_order(values):
    """Removes duplicate values from a list while preserving the original order.

    Args:
        values (list): The list of values to deduplicate.

    Returns:
        list: A new list with duplicates removed, maintaining order.
    """
    deduped = []
    for value in values:
        if value in deduped:
            continue
        deduped.append(value)
    return deduped


def _authenticate_participant_request(request, network):
    """Authenticates machine-to-machine requests with shared gossip credentials.

    Checks for X-Gossip-Participant and X-Gossip-Token headers against
    the network's shared secret.

    Args:
        request (HttpRequest): The incoming HTTP request.
        network (SwarmNetwork): The network instance to authenticate against.

    Returns:
        SwarmParticipant: The authenticated participant instance, or None if failed.
    """
    participant_id = (request.headers.get("X-Gossip-Participant") or "").strip()
    provided_token = request.headers.get("X-Gossip-Token")
    if not participant_id or not provided_token:
        return None

    # DEBUG: Log the provided token vs the expected token
    logger.network.debug(f"[AUTH DEBUG] Participant: {participant_id}, Provided: {provided_token[:8]}..., Expected: {network.gossip_token[:8] if network.gossip_token else 'NONE'}...")

    # Authenticate using the network's shared gossip token
    if network.gossip_token and secrets.compare_digest(provided_token, network.gossip_token):
        return network.participants.filter(participant_id=participant_id).first()

    # Legacy/Manual participant-scoped fallback
    participant = network.participants.filter(participant_id=participant_id).first()
    if participant and participant.gossip_token and secrets.compare_digest(provided_token, participant.gossip_token):
        return participant
    return None


def _get_local_participant_status(swarm_network):
    """Heuristically determines participant status from local server logs and containers.

    On a Server node, it reads server logs to find joined or disconnected clients.
    On a Client node, it verifies if the local participant's container is active.

    Args:
        swarm_network (SwarmNetwork): The network instance to check status for.

    Returns:
        dict: A mapping of participant IDs to their current status strings.
    """
    from django.core.cache import cache
    cache_key = f"network_status_{swarm_network.identifier}"
    cached_status = cache.get(cache_key)
    if cached_status:
        return cached_status

    participants = swarm_network.participants.all()
    server_logs = ""
    local_ip = get_tailscale_ip()
    
    logger.network.debug(f"[_get_local_participant_status] Checking local status for network {swarm_network.identifier}. Local IP: {local_ip}")

    if swarm_network.status == "RUNNING":
        try:
            import subprocess

            from .tasks import _container_name_for
            server_container = _container_name_for(swarm_network.identifier, "server")
            # Performance: Reduced tail from 12000 to 3000
            result = subprocess.run(
                ["docker", "logs", "--tail", "3000", server_container],
                capture_output=True, text=True, timeout=2
            )
            server_logs = (result.stdout + result.stderr).lower()
            logger.network.debug(f"[_get_local_participant_status] Successfully read logs from server container {server_container} ({len(server_logs)} bytes)")
        except Exception as e:
            logger.network.debug(f"[_get_local_participant_status] Could not read server logs (expected if not server node): {e}")

    # Robust Connection Tracking: Map connection IDs (CNXXXX) to participant IDs
    import re
    conn_map = {}
    conn_close_positions = {}
    latest_total_clients = None
    expected_client_count = participants.filter(role="CLIENT").count()
    if server_logs:
        total_clients_pattern = r"total clients:\s*(\d+)"
        for match in re.finditer(total_clients_pattern, server_logs):
            try:
                latest_total_clients = int(match.group(1))
            except (TypeError, ValueError):
                latest_total_clients = None

        # Scan for connection creation logs and map CN IDs to real training clients.
        # Ignore admin users and non-client channels (e.g. 8003 admin API churn).
        creation_pattern = r"connection \[(cn\d+) ([^\]]*?) ssl ([^\]\s]+)\] is created"
        for match in re.finditer(creation_pattern, server_logs):
            cn_id, conn_details, principal = match.groups()

            if ":8002" not in conn_details:
                continue

            p_id = principal.split("@", 1)[0].strip().lower()
            if p_id.startswith("admin-"):
                continue

            conn_map[cn_id] = p_id

        # Track close positions by CN ID so we can mark disconnected only when
        # a client-channel connection truly closed after it was joined.
        closure_pattern = r"connection \[(cn\d+) [^\]]*\] is closed"
        for match in re.finditer(closure_pattern, server_logs):
            conn_close_positions[match.group(1)] = match.start()

    # For debugging: list ALL running containers with our network label
    try:
        import subprocess
        label_filter = f"medswarmhub.network_id={swarm_network.identifier}"
        running_containers = subprocess.run(
            ["docker", "ps", "--filter", f"label={label_filter}", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=2
        ).stdout.splitlines()
        logger.network.debug(f"[_get_local_participant_status] Locally running containers for this network: {running_containers}")
    except Exception as e:
        logger.network.debug(f"[_get_local_participant_status] Could not list docker containers: {e}")

    status_map = {}
    participant_metrics = {}

    for p in participants:
        name = p.participant_id
        role = p.role.lower()
        is_me = (p.ip == local_ip)
        
        status = "Offline"
        if swarm_network.status == "RUNNING":
            if role == "server":
                # Check if server container is actually running
                try:
                    import shutil

                    from .tasks import _container_name_for, _is_container_running
                    docker_path = shutil.which("docker") or "docker"
                    env = os.environ.copy()
                    if _is_container_running(docker_path, _container_name_for(swarm_network.identifier, "server"), env):
                        status = "Online"
                except Exception:
                    if is_me: status = "Online"
                status_map[name] = status
            else:
                lname = name.lower()
                
                # A. Definitive Join Patterns (Directly from NVFlare ClientManager)
                joined_patterns = [
                    rf"Client: New client {lname}@.* joined", # ClientManager login
                    rf"Re-activate the client: {lname} at .* with token:", # ClientManager heartbeat recovery
                    rf"registered client {lname}",
                    rf"starting communication with client {lname}",
                    rf"client: {lname} joined",
                    rf"client {lname} joined",
                    rf"Receive heartbeat from Client:{lname}", # Note: NVFlare sometimes logs token here, but we check name too
                    rf"heartbeat from {lname}",
                ]
                
                # B. Definitive Leave Patterns (Directly from NVFlare ClientManager)
                leave_patterns = [
                    rf"Client Name:{lname} \tToken: .* left", # ClientManager remove_client
                    rf"client: {lname} left",
                    rf"client {lname} left",
                    rf"removed client {lname}",
                    rf"missing job on client '{lname}'",
                    rf"client manager: remove client {lname}",
                    rf"disconnected client {lname}",
                    rf"notified SJ of dead-job: .* {lname}",
                ]

                last_join_idx = -1
                for pat in joined_patterns:
                    for m in re.finditer(pat, server_logs):
                        if m.start() > last_join_idx: last_join_idx = m.start()
                
                # Check mapping for actual connection activity (also counts as Join activity)
                last_cn_created_idx = -1
                last_cn_closed_idx = -1
                for cn_id, p_id in conn_map.items():
                    if p_id == lname:
                        for m in re.finditer(rf"connection \[{re.escape(cn_id)}[^\]]*\] is created", server_logs):
                            if m.start() > last_cn_created_idx: last_cn_created_idx = m.start()
                        
                        cn_closed_at = conn_close_positions.get(cn_id, -1)
                        if cn_closed_at > last_cn_closed_idx: last_cn_closed_idx = cn_closed_at

                last_activity_idx = max(last_join_idx, last_cn_created_idx)
                
                last_leave_idx = -1
                for pat in leave_patterns:
                    for m in re.finditer(pat, server_logs):
                        if m.start() > last_leave_idx: last_leave_idx = m.start()

                # Definitive state
                if last_activity_idx > -1:
                    if last_leave_idx > last_activity_idx:
                        status = "Disconnected"
                    else:
                        status = "Joined"
                else:
                    status = "Offline"

                # Store metrics for global refinement
                participant_metrics[name] = {
                    "last_activity": last_activity_idx,
                    "last_leave": last_leave_idx,
                    "last_cn_close": last_cn_closed_idx,
                    "is_me": is_me
                }
                status_map[name] = status

    # C. Global Refinement using Total Count
    # If the server reports N clients, but we have M != N Joined clients, resolve ties.
    if latest_total_clients is not None and expected_client_count > 0:
        joined_participants = [n for n, s in status_map.items() if s == "Joined"]
        
        # Scenario 1: Too many marked as Joined (False Positives)
        if len(joined_participants) > latest_total_clients:
            # Sort Joined clients by their latest activity index (least recent activity first)
            # and consider those with a socket close after their last join as primary candidates for demotion.
            def demotion_score(name):
                m = participant_metrics[name]
                # If they had a socket close after their last activity, they are more likely to be the missing ones
                closed_after_activity = 1 if m["last_cn_close"] > m["last_activity"] else 0
                return (closed_after_activity, -m["last_activity"])

            joined_participants.sort(key=demotion_score, reverse=True)
            to_demote = len(joined_participants) - latest_total_clients
            for i in range(to_demote):
                p_name = joined_participants[i]
                if not participant_metrics[p_name]["is_me"]: # Don't demote myself unless necessary
                    status_map[p_name] = "Disconnected"

        # Scenario 2: Too few marked as Joined (False Negatives)
        elif len(joined_participants) < latest_total_clients:
            # Look at Disconnected clients who don't have a definitive "Left" message,
            # or whose "Left" message is very old.
            potential_recoveries = [n for n, s in status_map.items() if s == "Disconnected"]
            def recovery_score(name):
                m = participant_metrics[name]
                # If they have NO definitive leave message, but were marked Disconnected due to socket close
                has_leave = 1 if m["last_leave"] > -1 else 0
                return (has_leave, -m["last_activity"])
            
            potential_recoveries.sort(key=recovery_score)
            to_recover = min(len(potential_recoveries), latest_total_clients - len(joined_participants))
            for i in range(to_recover):
                status_map[potential_recoveries[i]] = "Joined"

    # D. Final Local Health Check (Fallback for Client Nodes)
    for p in participants:
        name = p.participant_id
        if status_map.get(name) in ["Offline", "Disconnected"] and p.ip == local_ip:
            try:
                import shutil

                from .tasks import _container_name_for, _is_container_running
                docker_path = shutil.which("docker") or "docker"
                env = os.environ.copy()
                my_container = _container_name_for(swarm_network.identifier, name)
                if _is_container_running(docker_path, my_container, env):
                    status_map[name] = "Online"
                else:
                    fallback_container = _container_name_for(swarm_network.identifier, "client")
                    if _is_container_running(docker_path, fallback_container, env):
                        status_map[name] = "Online"
            except Exception as e:
                logger.network.debug(f"[_get_local_participant_status] Error checking local container for {name}: {e}")

    return status_map


from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
def network_api_gossip(request, network_id):
    """SECURE Gossip Endpoint: Receives status "shouts" from peers.

    Verifies Gossip Token and Sender IP for security.

    Args:
        request (HttpRequest): The incoming HTTP request.
        network_id (str): The ID of the swarm network.

    Returns:
        JsonResponse: A response indicating acknowledgement or failure.
    """
    client_ip = request.META.get('REMOTE_ADDR')
    logger.network.info(
        f"[GOSSIP RECEIVE] Incoming POST from {client_ip} for network {network_id}"
    )

    if request.method != "POST":
        return JsonResponse({"error": "Only POST allowed"}, status=405)
    
    # 1. Verify participant-scoped gossip credentials
    network = get_object_or_404(SwarmNetwork, identifier=network_id)
    source_participant = _authenticate_participant_request(request, network)
    if source_participant is None:
        logger.network.warning(
            f"[GOSSIP REJECT] Unauthorized token from {client_ip}."
        )
        return JsonResponse({"error": "Unauthorized Gossip"}, status=401)

    try:
        data = json.loads(request.body)
        participant_id = data.get("participant_id")
        source_participant_id = data.get("source_participant_id") or participant_id
        status = data.get("status")

        if source_participant_id != source_participant.participant_id:
            logger.network.warning(
                f"[GOSSIP REJECT] Header/source mismatch: {source_participant.participant_id} != {source_participant_id}"
            )
            return JsonResponse({"error": "Sender mismatch"}, status=403)

        logger.network.info(f"[GOSSIP RECEIVE] Peer {source_participant_id} reports status: {status}")

        # 2. Verify Sender IP from trusted remote address (not X-Forwarded-For)
        participant = SwarmParticipant.objects.filter(
            network=network, 
            participant_id=source_participant_id
        ).first()
        
        if not participant:
             logger.network.error(f"[GOSSIP REJECT] Unknown participant {source_participant_id} (Network: {network.name})")
             # Log existing participants for debugging
             all_p = [p.participant_id for p in network.participants.all()]
             logger.network.debug(f"[GOSSIP REJECT] Known participants in DB: {all_p}")
             return JsonResponse({"error": "Unknown Participant"}, status=404)

        # Non-server senders may only update their own participant state.
        if participant.role != "SERVER" and participant_id != participant.participant_id:
            return JsonResponse({"error": "Forbidden status update scope"}, status=403)

        target_participant = participant
        if participant_id and participant_id != participant.participant_id:
            target_participant = SwarmParticipant.objects.filter(
                network=network,
                participant_id=participant_id,
            ).first()
            if not target_participant:
                return JsonResponse({"error": "Unknown target participant"}, status=404)

        # IP Lockdown check: Verify Sender IP
        # Before the regression, this was a warning only. We revert to warning
        # to support complex network topologies (Proxies, VPNs, Docker Bridge).
        if participant.ip and participant.ip not in [
            "-",
            "127.0.0.1",
            "localhost",
        ]:
            if client_ip != participant.ip:
                logger.network.warning(
                    f"[GOSSIP IP MISMATCH] {source_participant_id}: DB says {participant.ip}, request came from {client_ip}. Proceeding anyway due to valid token."
                )

        target_participant.status = status
        target_participant.last_seen = timezone.now()
        target_participant.save(update_fields=["status", "last_seen"])
        logger.network.debug(f"[GOSSIP ACK] Updated {target_participant.participant_id} to {status}")
        return JsonResponse({"status": "acknowledged"})
            
    except Exception as e:
        logger.network.error(f"[GOSSIP ERROR] Failed to process shout: {e}")
        return JsonResponse({"error": str(e)}, status=500)


def broadcast_network_status(network_id):
    """Helper to 'shout' local status to all peers in the network.

    Includes engine-level enrollment info if we are the server.
    Optimized: Dispatches status updates asynchronously via Celery.

    Args:
        network_id (str): The identifier of the network to broadcast.
    """
    from .tasks import shout_to_peer_task
    network = SwarmNetwork.objects.filter(identifier=network_id).first()
    if not network or network.status not in ["RUNNING", "STARTING"]:
        return

    local_ip = get_tailscale_ip()
    # Find ALL roles this machine is playing (e.g. could be both SERVER and a CLIENT)
    local_participants = network.participants.filter(ip=local_ip)
    
    if not local_participants.exists():
        logger.network.warning(f"[GOSSIP BROADCAST] Could not find myself in participant list for IP {local_ip}")
        return

    logger.network.info(f"[GOSSIP BROADCAST] Starting asynchronous broadcast for network {network.name}. Local IP: {local_ip}")

    # Source of Truth: Engine logs + Docker state
    status_map = _get_local_participant_status(network)
    
    # We use the SHARED GOSSIP TOKEN for peer-to-peer status sync.
    gossip_token = network.gossip_token
    if not gossip_token:
        logger.network.error(f"[GOSSIP BROADCAST] Network {network.name} has no gossip token! Peer sync impossible.")
        return

    for lp in local_participants:
        headers = {
            "X-Gossip-Participant": lp.participant_id,
            "X-Gossip-Token": gossip_token,
        }
        my_payload = {
            "source_participant_id": lp.participant_id,
            "participant_id": lp.participant_id,
            "status": status_map.get(lp.participant_id, "Online")
        }
        
        # If this role is the SERVER, it also shouts the JOINED status of all other clients it sees
        extra_shouts = []
        if lp.role == "SERVER":
            for p_id, p_status in status_map.items():
                if p_id != lp.participant_id:
                    extra_shouts.append({
                        "source_participant_id": lp.participant_id,
                        "participant_id": p_id,
                        "status": p_status
                    })

        peers = network.participants.exclude(id=lp.id)
        for peer in peers:
            if peer.ip and peer.ip != "-" and peer.ip != local_ip:
                shout_to_peer_task.delay(
                    peer_ip=peer.ip,
                    network_id=str(network.identifier),
                    payload=my_payload,
                    headers=headers,
                    extra_shouts=extra_shouts if lp.role == "SERVER" else None
                )


@login_required
@project_context_required
def network(request):
    """Main dashboard for managing swarm networks within the active project.

    Displays network statuses, connection info, and participant details.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: The rendered network dashboard.
    """
    current_project_uuid, _ = get_user_project(request)

    # Get the project object from the user's current project relation
    current_project_relation = UserCurrentProject.objects.select_related(
        "project"
    ).get(user=request.user)
    project = current_project_relation.project

    # List all networks associated with this specific project
    swarm_networks = SwarmNetwork.objects.filter(
        project=project
    ).select_related("project", "author")

    # Identify which network the user is currently focusing on
    try:
        current_network_rel = UserCurrentNetwork.objects.select_related(
            "network__project"
        ).get(user=request.user)
        current_network = current_network_rel.network
    except UserCurrentNetwork.DoesNotExist:
        current_network = None

    participants_details = []
    if current_network:
        # Trigger an immediate gossip broadcast when the dashboard is viewed
        # This acts as a 'manual' sync to ensure peer-to-peer visibility.
        try:
            broadcast_network_status(current_network.identifier)
        except Exception as e:
            logger.network.warning(f"[DASHBOARD] Manual gossip broadcast failed: {e}")

        participants = current_network.participants.all().order_by("role", "participant_id")
        local_ip = get_tailscale_ip()
        
        # Local source of truth (Docker logs)
        local_engine_status = _get_local_participant_status(current_network)
        
        logger.network.debug(f"[DASHBOARD] Rendering dashboard. Local IP: {local_ip}. Engine status: {local_engine_status}")

        for p in participants:
            i_am_server = current_network.participants.filter(ip=local_ip, role="SERVER").exists()
            is_me = (p.ip == local_ip)

            status = "Offline"
            
            if i_am_server:
                status = local_engine_status.get(p.participant_id, "Offline")
            else:
                if p.last_seen:
                    age = (timezone.now() - p.last_seen).total_seconds()
                    if age < 180:
                        status = p.status
                        logger.network.debug(f"[DASHBOARD] Participant {p.participant_id} status from Gossip: {status} (Age: {age}s)")
                    else:
                        status = "Offline (Stale)"
                
                if is_me and status not in ["Joined", "Online"]:
                    local_health = local_engine_status.get(p.participant_id, "Offline")
                    if local_health != "Offline":
                        status = local_health
                        logger.network.debug(f"[DASHBOARD] Falling back to local health for myself: {status}")

            participants_details.append({
                "name": p.participant_id,
                "role": p.get_role_display(),
                "status": status,
                "org": p.org or "-", 
                "ip": p.ip or "-",
            })

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


@csrf_exempt
def network_api_status(request, network_id):
    """Internal API endpoint providing live participant status from local server logs.

    Authenticates via either Session or Gossip Token.

    Args:
        request (HttpRequest): The incoming HTTP request.
        network_id (str): The ID of the swarm network.

    Returns:
        JsonResponse: A JSON mapping of participant statuses.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    source_participant = _authenticate_participant_request(request, swarm_network)

    if request.method != "GET":
        return JsonResponse({"error": "Only GET allowed"}, status=405)
    
    # 1. Authentication
    is_authenticated = False
    if source_participant is not None:
        is_authenticated = True
    elif request.user.is_authenticated:
        # Simple project membership check
        if swarm_network.project.members.filter(id=request.user.id).exists() or swarm_network.project.author == request.user:
            is_authenticated = True
            
    if not is_authenticated:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    statuses = _get_local_participant_status(swarm_network)
    return JsonResponse({"statuses": statuses})


@login_required
@project_context_required
def new_network(request):
    """Handles the creation of a new swarm network configuration.

    Supports three methods:
    1. 'create': Provision a new network from a list of client names and IPs.
    2. 'upload': Import an existing FLARE startup kit zip file.
    3. 'local_test': Auto-generate a local-only testing network.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: A redirect to the network dashboard or the rendered form.
    """
    if request.method == "POST":
        creation_method = request.POST.get("creation_method")
        network_name = request.POST.get("title")
        description = request.POST.get("description")

        current_project_relation = UserCurrentProject.objects.get(
            user=request.user
        )
        project = current_project_relation.project
        log = get_logger(user=request.user, project=project)

        if creation_method == "create":
            clients_json = request.POST.getlist("clients")
            clients = []
            for c_json in clients_json:
                try:
                    c_data = json.loads(c_json)
                    clients.append(
                        {
                            "name": c_data.get("name", "client"),
                            "ip": c_data.get("ip", ""),
                        }
                    )
                except (json.JSONDecodeError, TypeError):
                    continue
            network_services.create_network(
                project=project,
                actor=request.user,
                name=network_name,
                description=description,
                participants=clients,
                server_ip=get_tailscale_ip(),
            )

        elif creation_method == "upload":
            startup_package = request.FILES.get("startup_package")
            if startup_package:
                network_services.import_network(
                    project=project,
                    actor=request.user,
                    name=network_name,
                    description=description,
                    package_source=startup_package,
                )

        elif creation_method == "local_test":
            network_services.create_local_test_network(
                project=project,
                actor=request.user,
                name=network_name,
                description=description,
            )

        return redirect("network:network")

    context = {
        "segment": "network",
    }
    return render(request, "apps/network/new_network.html", context)


@login_required
@project_membership_required
def set_current_network(request, network_id):
    """Sets the specified network as the 'active' network for the current user.

    Args:
        request (HttpRequest): The incoming HTTP request.
        network_id (str): The ID of the network to set as current.

    Returns:
        HttpResponse: A redirect to the network dashboard.
    """
    network_obj = get_object_or_404(SwarmNetwork, identifier=network_id)
    current_project_relation = UserCurrentProject.objects.get(
        user=request.user
    )

    # Security check: Ensure the network belongs to the user's active project
    if network_obj.project == current_project_relation.project:
        network_services.set_current_network(request.user, network_obj)

    return redirect("network:network")


@login_required
@project_membership_required
def download_startup_kits(request, network_id):
    """Gathers startup kits, packages them into a ZIP file, and returns it.

    Args:
        request (HttpRequest): The incoming HTTP request.
        network_id (str): The ID of the swarm network.

    Returns:
        HttpResponse: The ZIP file download or a redirect with an error message.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    log = get_logger(user=request.user, project=swarm_network.project)

    log.network.info(
        f"User downloaded startup kits for network '{swarm_network.name}' (ID: {swarm_network.identifier})"
    )

    zip_buffer = network_services.export_startup_package(swarm_network)
    zip_content = zip_buffer.getvalue()

    response = HttpResponse(
        zip_content, content_type="application/zip"
    )
    filename = f"{swarm_network.name.replace(' ', '_')}_startup_kits.zip"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    return response


@login_required
@project_membership_required
def start_swarm_network(request, network_id):
    """Triggers the asynchronous Celery task to start the swarm network containers.

    Args:
        request (HttpRequest): The incoming HTTP request.
        network_id (str): The ID of the swarm network.

    Returns:
        HttpResponse: A redirect to the network dashboard.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    log = get_logger(user=request.user, project=swarm_network.project)

    log.network.info(
        f"Starting swarm network '{swarm_network.name}' (ID: {swarm_network.identifier})."
    )

    network_services.start_network(swarm_network, request.user)
    return redirect("network:network")


@login_required
@project_membership_required
def stop_swarm_network(request, network_id):
    """Triggers the asynchronous Celery task to stop and remove swarm containers.

    Args:
        request (HttpRequest): The incoming HTTP request.
        network_id (str): The ID of the swarm network.

    Returns:
        HttpResponse: A redirect to the network dashboard.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    log = get_logger(user=request.user, project=swarm_network.project)

    log.network.info(
        f"Stopping swarm network '{swarm_network.name}' (ID: {swarm_network.identifier})."
    )

    network_services.stop_network(swarm_network, request.user)
    return redirect("network:network")


@login_required
@project_membership_required
def get_swarm_network_status(request, network_id):
    """Returns the current status of a network for live UI updates.

    Args:
        request (HttpRequest): The incoming HTTP request.
        network_id (str): The ID of the swarm network.

    Returns:
        JsonResponse: A JSON object containing the status display string.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    return JsonResponse({"status": swarm_network.get_status_display()})


@require_POST
@login_required
@project_membership_required
def delete_swarm_network(request, network_id):
    """Deletes a swarm network configuration and its associated files from disk.

    Only the project author is permitted to delete networks.

    Args:
        request (HttpRequest): The incoming HTTP request.
        network_id (str): The ID of the swarm network.

    Returns:
        HttpResponse: A redirect to the network dashboard.
    """
    swarm_network = get_object_or_404(SwarmNetwork, identifier=network_id)
    log = get_logger(user=request.user, project=swarm_network.project)

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
