"""
Views for the network app.
Handles the orchestration of swarm networks, including provisioning,
deployment (start/stop), status monitoring, and startup kit distribution.
"""

import json
import os
import secrets
import shutil
import zipfile
import requests

import yaml
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST
from logs.logger import get_logger
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
from common.utils import get_safe_slug

# Standard project-wide logger initialization
logger = get_logger()


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


def _dedupe_keep_order(values):
    deduped = []
    for value in values:
        if value in deduped:
            continue
        deduped.append(value)
    return deduped


def _authenticate_participant_request(request, network):
    """Authenticate machine-to-machine requests with participant-scoped credentials."""
    participant_id = (request.headers.get("X-Gossip-Participant") or "").strip()
    provided_token = request.headers.get("X-Gossip-Token")
    if not participant_id or not provided_token:
        return None

    participant = network.participants.filter(participant_id=participant_id).first()
    if not participant or not participant.gossip_token:
        return None

    if secrets.compare_digest(provided_token, participant.gossip_token):
        return participant
    return None


def _get_local_participant_status(swarm_network):
    """
    Heuristic to determine participant status from local server logs and container state.
    On a Server node, it reads server logs to find Joined/Disconnected clients.
    On a Client node, it verifies if the local participant's container is alive.
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
            from .tasks import _container_name_for
            import subprocess
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
                    from .tasks import _is_container_running, _container_name_for
                    import shutil
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
                from .tasks import _is_container_running, _container_name_for
                import shutil
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
    """
    SECURE Gossip Endpoint: Receives status "shouts" from peers.
    Verifies Gossip Token and Sender IP for security.
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

        # IP Lockdown log
        if participant.ip and participant.ip not in [
            "-",
            "127.0.0.1",
            "localhost",
        ]:
            if client_ip != participant.ip:
                logger.network.warning(
                    f"[GOSSIP REJECT] IP mismatch for {source_participant_id}: expected {participant.ip}, got {client_ip}"
                )
                return JsonResponse({"error": "IP mismatch"}, status=403)

        target_participant.status = status
        target_participant.last_seen = timezone.now()
        target_participant.save(update_fields=["status", "last_seen"])
        logger.network.debug(f"[GOSSIP ACK] Updated {target_participant.participant_id} to {status}")
        return JsonResponse({"status": "acknowledged"})
            
    except Exception as e:
        logger.network.error(f"[GOSSIP ERROR] Failed to process shout: {e}")
        return JsonResponse({"error": str(e)}, status=500)


def broadcast_network_status(network_id):
    """
    Helper to 'shout' local status to all peers in the network.
    Includes engine-level enrollment info if we are the server.
    Optimized: Dispatches status updates asynchronously via Celery.
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
    for lp in local_participants:
        if not lp.gossip_token:
            lp.gossip_token = secrets.token_hex(32)
            lp.save(update_fields=["gossip_token"])
        headers = {
            "X-Gossip-Participant": lp.participant_id,
            "X-Gossip-Token": lp.gossip_token,
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
    """
    Main dashboard for managing swarm networks within the active project.
    Displays network statuses, connection info, and participant details.
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
    """
    Internal API endpoint providing live participant status from local server logs.
    Authenticates via either Session or Gossip Token.
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
        log = get_logger(user=request.user, project=project)

        # We create the record inside each method to allow identifier recovery for uploads
        swarm_network = None

        if creation_method == "create":
            swarm_network = SwarmNetwork.objects.create(
                name=network_name,
                project=project,
                description=description,
                author=request.user,
                creation_method="CREATED",
            )
            log.network.info(
                f"Initialized new network record: {network_name} (ID: {swarm_network.identifier})"
            )

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
                org="swarm_control_plane",
                ip=server_ip,
            )
            for client_data in clients:
                SwarmParticipant.objects.create(
                    network=swarm_network,
                    user=request.user,
                    role="CLIENT",
                    participant_id=client_data["name"],
                    org=f"org_{client_data['name'].replace('-', '_')}",
                    ip=client_data["ip"],
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
                # OPTIMIZATION: Extract original network identifier from zip
                original_network_id = None
                try:
                    with zipfile.ZipFile(startup_package, "r") as zip_peek:
                        file_list = zip_peek.namelist()
                        
                        # 1. Check for dedicated .network_id file (highest priority)
                        if ".network_id" in file_list:
                            original_network_id = zip_peek.read(".network_id").decode("utf-8").strip()
                        
                        # 2. Fallback: Parse path if it was compressed with parent folders
                        if not original_network_id:
                            for name in file_list:
                                if name.startswith("workspaces/"):
                                    parts = name.split("/")
                                    if len(parts) >= 3:
                                        original_network_id = parts[2]
                                        break
                except Exception as peek_err:
                    log.network.warning(f"Failed to peek into zip for identifier: {peek_err}")

                create_args = {
                    "name": network_name,
                    "project": project,
                    "description": description,
                    "author": request.user,
                    "creation_method": "UPLOADED",
                }
                if original_network_id:
                    try:
                        import uuid
                        uuid.UUID(original_network_id) # Verify format
                        create_args["identifier"] = original_network_id
                        log.network.info(f"Recovered original network identifier: {original_network_id}")
                    except Exception: pass

                # Create the database record
                swarm_network = SwarmNetwork.objects.create(**create_args)
                log.network.info(f"Initialized uploaded network record: {network_name} (ID: {swarm_network.identifier})")

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
                        if member_path == "runtime_requirements.txt":
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
                            "runtime_requirements.txt",
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

                # Ensure uploaded kits keep a resolvable remote server host for FLARE admin connections.
                resolved_server_host = ""
                try:
                    env_host = os.getenv("MEDSWARMHUB_SERVER_HOST", "").strip()
                    if env_host:
                        resolved_server_host = env_host

                    root_host_file = os.path.join(prod_00_dir, "startup", "server_host.txt")
                    if not resolved_server_host and os.path.exists(root_host_file):
                        resolved_server_host = open(root_host_file).read().strip()

                    if not resolved_server_host:
                        project_yml_path = os.path.join(provision_dir, "project.yml")
                        if os.path.exists(project_yml_path):
                            with open(project_yml_path) as f:
                                project_yml = yaml.safe_load(f) or {}
                            for participant in project_yml.get("participants", []):
                                participant_name = str(participant.get("name", "")).strip().lower()
                                listening_host = str(participant.get("listening_host", "")).strip()
                                if participant_name.startswith("server") and listening_host:
                                    if listening_host.lower() not in {
                                        "dynamic",
                                        "localhost",
                                        "127.0.0.1",
                                        "server",
                                    }:
                                        resolved_server_host = listening_host
                                        break

                    # Fallback: derive from fed_client endpoint when available.
                    if not resolved_server_host:
                        fed_client_json = os.path.join(prod_00_dir, "startup", "fed_client.json")
                        if os.path.exists(fed_client_json):
                            with open(fed_client_json) as f:
                                cfg = json.load(f) or {}
                            # Try to extract the server endpoint from the High Availability agent configuration (NVFlare uses 'overseer_agent' key)
                            ha_agent_cfg = cfg.get("overseer_agent", {})
                            ha_agent_args = ha_agent_cfg.get("args", {})
                            endpoint = (
                                ha_agent_args.get("sp_end_point", "")
                                or ha_agent_args.get("overseer_end_point", "")
                            )
                            endpoint = str(endpoint).strip()
                            if endpoint:
                                if "://" in endpoint:
                                    from urllib.parse import urlparse

                                    resolved_server_host = (urlparse(endpoint).hostname or "").strip()
                                else:
                                    resolved_server_host = endpoint.split(":")[0].strip()

                                if resolved_server_host.lower() in {
                                    "",
                                    "dynamic",
                                    "localhost",
                                    "127.0.0.1",
                                    "server",
                                }:
                                    resolved_server_host = ""

                    if resolved_server_host:
                        for item in os.scandir(prod_00_dir):
                            if not item.is_dir():
                                continue
                            startup_dir = os.path.join(item.path, "startup")
                            if os.path.isdir(startup_dir):
                                with open(os.path.join(startup_dir, "server_host.txt"), "w") as f:
                                    f.write(resolved_server_host)

                        root_startup = os.path.join(prod_00_dir, "startup")
                        if os.path.isdir(root_startup):
                            with open(os.path.join(root_startup, "server_host.txt"), "w") as f:
                                f.write(resolved_server_host)

                        log.network.info(
                            f"Resolved uploaded startup kit server host: {resolved_server_host}"
                        )
                except Exception as e:
                    log.network.warning(
                        f"Could not derive server host from uploaded startup kit: {e}"
                    )

                # Mark as provisioned
                swarm_network.status = "PROVISIONED"
                
                # NEW: Recover Gossip Token and Participant List from uploaded zip if present
                discovered_participants = []
                try:
                    with zipfile.ZipFile(startup_package, "r") as zip_ref:
                        file_list = zip_ref.namelist()
                        
                        # A. Recover Participant Metadata (The Master List)
                        if ".participants.json" in file_list:
                            try:
                                p_data = json.loads(zip_ref.read(".participants.json").decode("utf-8"))
                                if isinstance(p_data, list):
                                    for p in p_data:
                                        p_id = p.get("participant_id")
                                        p_role = p.get("role")
                                        p_ip = p.get("ip", "-")
                                        
                                        # If the recovered IP is generic, try to use the resolved host as fallback for the server
                                        if p_ip.lower() in ["dynamic", "localhost", "127.0.0.1", "server"]:
                                            if p_role == "SERVER" and resolved_server_host:
                                                p_ip = resolved_server_host
                                            else:
                                                p_ip = "-"

                                        discovered_participants.append({
                                            "name": p_id,
                                            "role": p_role,
                                            "ip": p_ip,
                                            "org": p.get("org")
                                        })
                                    log.network.info(f"Recovered {len(discovered_participants)} participants from .participants.json")
                            except Exception as json_err:
                                log.network.warning(f"Failed to parse .participants.json: {json_err}")
                except Exception as zip_err:
                    log.network.warning(f"Failed to read metadata from zip: {zip_err}")
                
                swarm_network.save()

                # Populate SwarmParticipant records from the uploaded kit
                try:
                    # If we didn't find the JSON master list, fall back to legacy discovery
                    if not discovered_participants:
                        # 1. Parse project.yml for the full truth (Names + IPs + Roles)
                        project_yml_path = os.path.join(provision_dir, "project.yml")
                        if os.path.exists(project_yml_path):
                            with open(project_yml_path) as f:
                                yml = yaml.safe_load(f) or {}
                            for p in yml.get("participants", []):
                                p_name = str(p.get("name", "")).strip()
                                p_type = str(p.get("type", p.get("role", ""))).lower()
                                p_ip = str(p.get("listening_host", "")).strip()
                                if not p_name: continue
                                role = "CLIENT"
                                if p_type == "server": role = "SERVER"
                                if p_ip.lower() in ["dynamic", "localhost", "127.0.0.1", "server"]:
                                    if role == "SERVER" and resolved_server_host:
                                        p_ip = resolved_server_host
                                    else:
                                        p_ip = "-"
                                discovered_participants.append({"name": p_name, "role": role, "ip": p_ip, "org": None})

                        # 2. Check for local fed_client name
                        client_cfgs = [
                            os.path.join(prod_00_dir, "startup", "fed_client.json"),
                            os.path.join(provision_dir, "startup", "fed_client.json")
                        ]
                        for cfg_path in client_cfgs:
                            if os.path.exists(cfg_path):
                                try:
                                    with open(cfg_path) as f:
                                        data = json.load(f)
                                        c_name = data.get("client_name") or data.get("name")
                                        if c_name and c_name != "server":
                                            if not any(dp["name"] == c_name for dp in discovered_participants):
                                                discovered_participants.append({"name": c_name, "role": "CLIENT", "ip": "-", "org": None})
                                except Exception: pass

                    # Clear any existing stale participants
                    swarm_network.participants.all().delete()

                    for dp in discovered_participants:
                        SwarmParticipant.objects.create(
                            network=swarm_network,
                            user=request.user,
                            role=dp["role"],
                            participant_id=dp["name"],
                            ip=dp.get("ip", "-"),
                            org=dp.get("org") or f"org_{dp['name'].replace('-', '_')}"
                        )

                    if discovered_participants:
                        log.network.info(f"Registered {len(discovered_participants)} participant(s) with IP metadata.")
                except Exception as _pe:
                    log.network.warning(f"Could not populate participants from uploaded kit: {_pe}")

                log.network.info(
                    f"Startup kit extracted and network '{network_name}' marked as PROVISIONED."
                )

        elif creation_method == "local_test":
            swarm_network = SwarmNetwork.objects.create(
                name=network_name,
                project=project,
                description=description,
                author=request.user,
                creation_method="LOCAL_TEST",
            )
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
    log = get_logger(user=request.user, project=swarm_network.project)

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
    log = get_logger(user=request.user, project=swarm_network.project)

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
    log = get_logger(user=request.user, project=swarm_network.project)

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
