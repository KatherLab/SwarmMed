"""View functions for the training application.

Handles training job management, progress monitoring, and decentralized
communication between NVFlare nodes.
"""

import ast
import contextlib
import json
import os
import re
import secrets
import socket
import ssl
import threading
import time

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from common.utils import get_safe_slug
from logs.logger import get_logger
from network.models import SwarmNetwork
from project.decorators import (
    project_context_required,
    project_membership_required,
)
from project.models import UserCurrentProject

from . import services as training_services
from .models import TrainingJob
from .runtime import (
    build_training_status_payload as runtime_build_training_status_payload,
    dedupe_keep_order as runtime_dedupe_keep_order,
    find_latest_training_log as runtime_find_latest_training_log,
    format_duration as runtime_format_duration,
    get_training_progress_info as runtime_get_training_progress_info,
    log_flare_pre_submit_diagnostics as runtime_log_flare_pre_submit_diagnostics,
    new_secure_session_with_host as runtime_new_secure_session_with_host,
    nvflare_status_payload as runtime_nvflare_status_payload,
    parse_nvflare_clients as runtime_parse_nvflare_clients,
    parse_nvflare_jobs as runtime_parse_nvflare_jobs,
    resolve_admin_session_target as runtime_resolve_admin_session_target,
    tail_text as runtime_tail_text,
)
from .utils import (
    extract_flare_job_uuid,
    summarize_training_log,
    should_update_terminal_status,
)

logger = get_logger()


def _peer_tls_verify_path():
    """Determines the CA certificate path for peer-to-peer TLS verification.

    Can be overridden via SWARMMEDHUB_CA_CERT or disabled via
    SWARMMEDHUB_SKIP_PEER_SSL_VERIFY. Defaults to skipping verification (False)
    if not explicitly set to 'false'.

    Returns:
        str or bool: Path to CA cert or False if verification is skipped.
    """
    if os.getenv("SWARMMEDHUB_SKIP_PEER_SSL_VERIFY", "true").lower() in ("true", "1", "yes"):
        return False
    
    env_ca = os.getenv("SWARMMEDHUB_CA_CERT", "").strip()
    if env_ca:
        return env_ca
        
    return getattr(settings, "CA_CERT_PATH", "/usr/local/share/ca-certificates/internal-ca.crt")


def _authenticate_participant_request(request, network):
    """Authenticates a request from a participant node using gossip headers.

    Args:
        request (HttpRequest): The incoming request.
        network (SwarmNetwork): The network the participant belongs to.

    Returns:
        SwarmParticipant or None: The authenticated participant or None.
    """
    participant_id = (request.headers.get("X-Gossip-Participant") or "").strip()
    provided_token = request.headers.get("X-Gossip-Token")
    if not participant_id or not provided_token:
        return None

    # Authenticate using the network's shared gossip token
    if network.gossip_token and secrets.compare_digest(provided_token, network.gossip_token):
        return network.participants.filter(participant_id=participant_id).first()

    # Legacy/Manual participant-scoped fallback
    participant = network.participants.filter(participant_id=participant_id).first()
    if participant and participant.gossip_token and secrets.compare_digest(provided_token, participant.gossip_token):
        return participant
        
    return None


def training_api_state(request, network_id):
    """Internal API: Returns the latest training job state from this node.

    Used by client nodes to mirror the server node's training state.
    Authenticates via either Session or Gossip Token.

    Args:
        request (HttpRequest): The incoming request.
        network_id (str): The identifier of the network.

    Returns:
        JsonResponse: The latest training job state.
    """
    network = get_object_or_404(SwarmNetwork, identifier=network_id)
    source_participant = _authenticate_participant_request(request, network)
    
    # 1. Authentication
    is_authenticated = False
    if source_participant is not None:
        is_authenticated = True
    elif request.user.is_authenticated:
        # Simple project membership check
        if network.project.members.filter(id=request.user.id).exists() or network.project.author == request.user:
            is_authenticated = True
            
    if not is_authenticated:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    job = TrainingJob.objects.filter(network=network).order_by("-created_at").first()
    
    if not job:
        return JsonResponse({"job": None})
        
    return JsonResponse({
        "job": {
            "flare_job_id": job.flare_job_id,
            "flare_job_uuid": job.flare_job_uuid,
            "status": job.status,
            "total_rounds": job.total_rounds,
            "rounds_finished": job.rounds_finished,
            "progress_percent": job.progress_percent,
            "created_at": job.created_at.isoformat(),
        }
    })


def training_api_results(request, network_id):
    """Internal API: Provides the training results (aggregated model) as a download.

    Server node serves this to client nodes for decentralized sync.
    Authenticates via either Session or Gossip Token.

    Args:
        request (HttpRequest): The incoming request.
        network_id (str): The identifier of the network.

    Returns:
        JsonResponse or HttpResponse: The model file or error.
    """
    network = get_object_or_404(SwarmNetwork, identifier=network_id)
    source_participant = _authenticate_participant_request(request, network)
    
    # 1. Authentication
    is_authenticated = False
    if source_participant is not None:
        is_authenticated = True
    elif request.user.is_authenticated:
        if network.project.members.filter(id=request.user.id).exists() or network.project.author == request.user:
            is_authenticated = True
            
    if not is_authenticated:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    job = TrainingJob.objects.filter(network=network, status="COMPLETED").order_by("-created_at").first()
    
    if not job:
        return JsonResponse({"error": "No completed job found"}, status=404)
        
    # Logic to find the aggregated model file in the workspace
    job_uuid = (
        job.flare_job_uuid
        or extract_flare_job_uuid(job.flare_job_id)
        or str(job.flare_job_id)
    )
    
    workspace_root = os.path.join(settings.BASE_DIR, "workspaces", str(job.project.identifier), str(network.identifier), "workspace")
    model_path = None
    
    # Heuristic to find the best model file
    for root, _dirs, files in os.walk(workspace_root):
        if job_uuid in root:
            for f in files:
                if f in ["best_FL_model.pt", "model_weights.npz", "global_model.pt"]:
                    model_path = os.path.join(root, f)
                    break
        if model_path: break
        
    if not model_path or not os.path.exists(model_path):
        return JsonResponse({"error": "Model file not found"}, status=404)
        
    with open(model_path, "rb") as f:
        response = HttpResponse(f.read(), content_type="application/octet-stream")
        response["Content-Disposition"] = f'attachment; filename="{os.path.basename(model_path)}"'
        return response


def _extract_cert_common_name(cert_path: str) -> str:
    """Extracts the Common Name (CN) from an SSL certificate.

    Args:
        cert_path (str): Path to the certificate file.

    Returns:
        str: The common name if found, else an empty string.
    """
    cert_path = (cert_path or "").strip()
    if not cert_path or not os.path.exists(cert_path):
        return ""

    try:
        cert_info = ssl._ssl._test_decode_cert(cert_path)
        for rdn in cert_info.get("subject", []):
            for key, value in rdn:
                if str(key).strip().lower() == "commonname":
                    return str(value or "").strip()
    except Exception:
        return ""

    return ""


def _build_training_status_payload(current_network, job):
    """Compatibility wrapper for the shared training status payload builder."""
    return runtime_build_training_status_payload(current_network, job)


def _resolve_admin_startup_dir(current_network) -> str | None:
    """Resolves the directory containing the NVFlare admin startup kit.

    Args:
        current_network (SwarmNetwork): The network to resolve for.

    Returns:
        str or None: The absolute path to the admin startup directory or None.
    """
    if current_network.admin_startup_dir and os.path.exists(
        current_network.admin_startup_dir
    ):
        return current_network.admin_startup_dir

    override = os.environ.get("SWARMMEDHUB_NVFLARE_ADMIN_DIR", "").strip()
    if override and os.path.exists(override):
        return override

    project_name = get_safe_slug(
        current_network.project.title, current_network.project.identifier
    ).replace("-", "_")
    admin_user_dir = os.path.join(
        "workspaces",
        str(current_network.project.identifier),
        str(current_network.identifier),
        "workspace",
        project_name,
        "prod_00",
        "admin@nvidia.com",
        "startup",
    )
    if os.path.exists(admin_user_dir):
        return admin_user_dir

    prod_00_dir = os.path.join(
        "workspaces",
        str(current_network.project.identifier),
        str(current_network.identifier),
        "workspace",
        project_name,
        "prod_00",
    )
    if os.path.exists(prod_00_dir):
        for item in sorted(os.listdir(prod_00_dir)):
            if not item.startswith("admin-"):
                continue
            cand = os.path.join(prod_00_dir, item, "startup")
            if os.path.exists(cand):
                return cand

    return None


def _resolve_admin_session_target(current_network) -> tuple[str, str, str] | None:
    """Compatibility wrapper for shared admin session target resolution."""
    return runtime_resolve_admin_session_target(current_network)


def _parse_nvflare_jobs(response):
    """Compatibility wrapper for shared NVFlare job parsing."""
    return runtime_parse_nvflare_jobs(response)


def _select_nvflare_job(jobs):
    """Selects the most relevant (running or first) job from a list of NVFlare jobs.

    Args:
        jobs (list): A list of job dictionaries.

    Returns:
        dict or None: The selected job dictionary or None if the list is empty.
    """
    if not jobs:
        return None

    def status_of(job):
        return (
            str(
                job.get("status")
                or job.get("job_status")
                or job.get("state")
                or ""
            )
            .upper()
            .strip()
        )

    running = [j for j in jobs if status_of(j) == "RUNNING"]
    if running:
        return running[0]
    return jobs[0]


def _dedupe_keep_order(values):
    """Compatibility wrapper for shared dedupe helper."""
    return runtime_dedupe_keep_order(values)


_NVFLARE_GRPC_PATCHED: bool = False
_NVFLARE_GRPC_PATCH_LOCK = threading.Lock()

import logging as _stdlib_logging

_grpc_patch_log = _stdlib_logging.getLogger(__name__)


def _ensure_grpc_ssl_patched(tls_server_name: str) -> None:
    """Patches gRPC secure channel to allow SSL target name override.

    This is necessary for NVFlare admin API connections when the host IP
    doesn't match the certificate's common name.

    Args:
        tls_server_name (str): The server name to use for SSL verification.
    """
    global _NVFLARE_GRPC_PATCHED
    if _NVFLARE_GRPC_PATCHED:
        return
    with _NVFLARE_GRPC_PATCH_LOCK:
        if _NVFLARE_GRPC_PATCHED:
            return
        try:
            import grpc as _grpc
            _orig = _grpc.secure_channel

            def _patched(target, credentials, options=None, **kwargs):
                opts = list(options or [])
                if not any(
                    isinstance(o, (list, tuple)) and len(o) >= 1
                    and o[0] == "grpc.ssl_target_name_override"
                    for o in opts
                ):
                    opts.append(("grpc.ssl_target_name_override", tls_server_name))
                return _orig(target, credentials, options=opts, **kwargs)

            _grpc.secure_channel = _patched
            _NVFLARE_GRPC_PATCHED = True
            _grpc_patch_log.debug(
                "[FLARE] grpc.secure_channel patched with "
                "ssl_target_name_override=%r", tls_server_name
            )
        except Exception as e:
            _grpc_patch_log.warning("[FLARE] grpc.secure_channel patch failed: %s", e)


def _build_flare_host_candidates(requested_host: str, default_host: str = ""):
    """Builds a prioritized list of IP/Hostname candidates for FLARE API connection.

    Args:
        requested_host (str): The host provided via configuration.
        default_host (str, optional): The default host from FLARE session.

    Returns:
        list: A deduplicated list of strings.
    """
    requested_host = (requested_host or "").strip()
    default_host = (default_host or "").strip()
    seed_candidates = ["server", requested_host, default_host, "127.0.0.1", "localhost", ""]
    return _dedupe_keep_order([c for c in seed_candidates if c is not None])


def _build_flare_port_candidates(default_port: int = 0):
    """Builds a list of potential ports for the FLARE admin API.

    Args:
        default_port (int, optional): The default port from FLARE session.

    Returns:
        list: A list of integers.
    """
    env_admin_port = os.getenv("SWARMMEDHUB_FLARE_ADMIN_PORT", "").strip()
    candidates = []
    if env_admin_port:
        with contextlib.suppress(ValueError):
            candidates.append(int(env_admin_port))
    if default_port and int(default_port) > 0 and int(default_port) not in candidates:
        candidates.append(int(default_port))
    if 8003 not in candidates:
        candidates.append(8003)
    if 8002 not in candidates:
        candidates.append(8002)
    return candidates


def _log_flare_pre_submit_diagnostics(log, username: str, startup_kit_location: str, requested_host: str):
    """Logs detailed connectivity and TLS diagnostics before job submission.

    Args:
        log: The logger instance.
        username (str): The NVFlare admin username.
        startup_kit_location (str): The workspace directory for the session.
        requested_host (str): The server host address to test.
    """
    try:
        from nvflare.fuel.flare_api.flare_api import Session
        startup_dir = os.path.join(startup_kit_location, "startup")
        fed_admin_path = os.path.join(startup_dir, "fed_admin.json")
        cert_cn = _extract_cert_common_name(os.path.join(startup_dir, "client.crt"))
        ca_cert_path = os.path.join(startup_dir, "rootCA.pem")
        client_cert_path = os.path.join(startup_dir, "client.crt")
        client_key_path = os.path.join(startup_dir, "client.key")

        probe_session = Session(
            username=username,
            startup_path=startup_kit_location,
            secure_mode=True,
            debug=False,
        )

        default_host = ""
        default_port = 0
        if probe_session.api:
            default_host = str(getattr(probe_session.api, "host", "") or "").strip()
            try:
                default_port = int(getattr(probe_session.api, "port", 0) or 0)
            except Exception:
                default_port = 0

        host_candidates = _build_flare_host_candidates(requested_host, default_host)
        port_candidates = _build_flare_port_candidates(default_port)

        log.training.info(
            "FLARE pre-submit diagnostics: "
            f"admin_dir={startup_kit_location}, "
            f"resolved_username={username}, "
            f"cert_cn={cert_cn or 'n/a'}, "
            f"startup_exists={os.path.isdir(startup_dir)}, "
            f"fed_admin_exists={os.path.exists(fed_admin_path)}, "
            f"default_host={default_host or 'n/a'}, "
            f"default_port={default_port or 'n/a'}, "
            f"host_candidates={host_candidates}, "
            f"port_candidates={port_candidates}, "
            f"ca_cert_exists={os.path.exists(ca_cert_path)}, "
            f"client_cert_exists={os.path.exists(client_cert_path)}, "
            f"client_key_exists={os.path.exists(client_key_path)}"
        )

        max_probes = 8
        probes = 0
        for host_candidate in host_candidates:
            effective_host = (host_candidate or default_host or "").strip()
            if not effective_host:
                continue
            for port in port_candidates:
                if probes >= max_probes:
                    break
                probes += 1
                reachable = False
                detail = ""
                try:
                    with socket.create_connection((effective_host, int(port)), timeout=2.0):
                        reachable = True
                except Exception as e:
                    detail = str(e)
                log.training.info(
                    "FLARE connectivity probe: "
                    f"host={effective_host} port={int(port)} reachable={reachable}"
                    + (f" detail={detail}" if detail else "")
                )
                if reachable and os.path.exists(ca_cert_path) and os.path.exists(client_cert_path) and os.path.exists(client_key_path):
                    tls_ok = False
                    tls_detail = ""
                    peer_cn = ""
                    peer_san = ""
                    try:
                        tls_ctx = ssl.create_default_context(cafile=ca_cert_path)
                        tls_ctx.check_hostname = False
                        tls_ctx.load_cert_chain(certfile=client_cert_path, keyfile=client_key_path)
                        with socket.create_connection((effective_host, int(port)), timeout=2.5) as raw_sock:
                            with tls_ctx.wrap_socket(raw_sock, server_hostname=effective_host) as tls_sock:
                                cert = tls_sock.getpeercert()
                                subject = cert.get("subject", []) if isinstance(cert, dict) else []
                                for subject_item in subject:
                                    for k, v in subject_item:
                                        if k == "commonName":
                                            peer_cn = str(v)
                                            break
                                    if peer_cn:
                                        break
                                san_entries = cert.get("subjectAltName", []) if isinstance(cert, dict) else []
                                if san_entries:
                                    peer_san = ",".join(
                                        f"{san_type}:{san_value}" for san_type, san_value in san_entries
                                    )
                                tls_ok = True
                    except Exception as e:
                        tls_detail = str(e)
                    log.training.info(
                        "FLARE TLS probe: "
                        f"host={effective_host} port={int(port)} tls_ok={tls_ok}"
                        + (f" peer_cn={peer_cn}" if peer_cn else "")
                        + (f" peer_san={peer_san}" if peer_san else "")
                        + (f" detail={tls_detail}" if tls_detail else "")
                    )
            if probes >= max_probes:
                break
        try:
            # Safe close for unconnected probe session
            if probe_session.api and not getattr(probe_session.api, "cell", None):
                probe_session.api.closed = True
            else:
                probe_session.close()
        except Exception:
            pass
    except Exception as e:
        log.training.warning(f"FLARE pre-submit diagnostics failed: {e}")


def _parse_nvflare_clients(response) -> list[str]:
    """Compatibility wrapper for shared NVFlare client parsing."""
    return runtime_parse_nvflare_clients(response)


def new_secure_session_with_host(username: str, startup_kit_location: str, host: str, debug: bool = False, timeout: float = 20.0, network_id=None):
    """Compatibility wrapper for shared secure-session creation."""
    return runtime_new_secure_session_with_host(
        username=username,
        startup_kit_location=startup_kit_location,
        host=host,
        debug=debug,
        timeout=timeout,
        network_id=network_id,
    )


def _nvflare_status_payload(current_network):
    """Compatibility wrapper for shared NVFlare status lookup."""
    return runtime_nvflare_status_payload(current_network)


def _tail_text(file_path: str, max_bytes: int = 2048 * 1024) -> str:
    """Compatibility wrapper for shared file-tail reading."""
    return runtime_tail_text(file_path, max_bytes=max_bytes)


def _find_latest_training_log(project_id: str, network_id: str, job_uuid: str, cache_ttl_seconds: int = 60) -> str | None:
    """Compatibility wrapper for shared latest-log lookup."""
    return runtime_find_latest_training_log(
        project_id, network_id, job_uuid, cache_ttl_seconds=cache_ttl_seconds
    )


def get_user_project(request):
    """Utility to extract the active project ID for the current request user.

    Args:
        request (HttpRequest): The incoming request.

    Returns:
        tuple: (project_id_str, success_bool)
    """
    try:
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        if not user_current_project.project:
            return None, False
        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False


def format_duration(seconds):
    """Compatibility wrapper for shared duration formatting."""
    return runtime_format_duration(seconds)


def get_training_progress_info(training_job, current_network):
    """Compatibility wrapper for shared progress/status derivation."""
    return runtime_get_training_progress_info(training_job, current_network)


@login_required
@project_context_required
def training(request):
    """Displays the main training dashboard for the active project.

    Args:
        request (HttpRequest): The incoming request.

    Returns:
        HttpResponse: The rendered dashboard page.
    """
    current_network = SwarmNetwork.resolve_current(request.user)
    if not current_network or current_network.status not in ["RUNNING", "STARTING", "PROVISIONED"]:
        return render(request, "apps/training/no_network_started.html", {"segment": "training"})

    is_training_running = False
    training_job = None
    training_progress = 0
    training_status = "Not started"
    training_logs = []
    duration_str = "-"
    eta_str = "-"

    if current_network:
        training_job = TrainingJob.objects.filter(network=current_network).order_by("-created_at").first()
        if training_job:
            info = get_training_progress_info(training_job, current_network)
            training_status = info["status"]
            training_progress = info["progress"]
            duration_str = info["duration"]
            eta_str = info["eta"]
            is_training_running = info["is_running"]
            try:
                job_uuid = (
                    training_job.flare_job_uuid
                    or extract_flare_job_uuid(training_job.flare_job_id)
                    or str(training_job.flare_job_id)
                )
                latest_log = _find_latest_training_log(
                    str(training_job.project.identifier),
                    str(current_network.identifier),
                    job_uuid,
                )
                if latest_log and os.path.exists(latest_log):
                    tail = _tail_text(latest_log, max_bytes=256 * 1024)
                    lines = tail.splitlines()[-50:]
                    for line in lines:
                        line = line.strip()
                        if not line: continue
                        ts = ""
                        level = ""
                        msg = line
                        logger_name = ""
                        ts_match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})", line)
                        if ts_match:
                            ts = ts_match.group(1)
                            remaining = line[len(ts) :].strip()
                            dash_parts = remaining.split(" - ")
                            if len(dash_parts) >= 3:
                                logger_name = dash_parts[0].strip(" -")
                                level = dash_parts[1].strip()
                                msg = " - ".join(dash_parts[2:])
                            else:
                                msg = remaining.strip(" -")
                        training_logs.append({"timestamp": ts, "level": level, "message": msg.strip(), "logger": logger_name})
                    training_logs.reverse()
            except Exception as e:
                logger.training.debug(f"Failed to collect training logs: {e}")

    context = {
        "segment": "training", "current_network": current_network, "is_training_running": is_training_running,
        "training_status": training_status, "training_progress": training_progress, "training_logs": training_logs,
        "duration_str": duration_str, "eta_str": eta_str,
    }
    return render(request, "apps/training/training.html", context)


@login_required
@project_membership_required
def start_training(request, network_id):
    """Assembles the federated learning job, and submits it to the swarm network.

    This function fetches training code from S3, handles project-specific 
    parameter injection, detects the framework (PyTorch/TF/NP), 
    and uses the NVFlare SDK to submit the job.

    Args:
        request (HttpRequest): The incoming request.
        network_id (uuid): The network to run the training on.

    Returns:
        HttpResponseRedirect: A redirect back to the training dashboard.
    """
    network = get_object_or_404(SwarmNetwork, identifier=network_id)
    try:
        training_job = training_services.submit_training_job(
            actor=request.user, network=network
        )
        messages.success(
            request, f"Successfully submitted job {training_job.flare_job_id}"
        )
    except Exception as e:
        messages.error(request, f"Failed to submit job: {e}")

    return redirect("training:training")


@login_required
@project_membership_required
def stop_training(request, network_id):
    """Aborts a currently running training job on the given network.

    Args:
        request (HttpRequest): The incoming request.
        network_id (uuid): The ID of the network.

    Returns:
        HttpResponseRedirect: A redirect back to the training dashboard.
    """
    network = get_object_or_404(SwarmNetwork, identifier=network_id)
    try:
        job = training_services.stop_training_job(
            actor=request.user, network=network
        )
        messages.success(request, f"Successfully aborted job {job.flare_job_id}")
    except LookupError as e:
        messages.warning(request, str(e))
    except Exception as e:
        messages.error(request, f"Failed to abort job: {e}")
    return redirect("training:training")


@login_required
@project_context_required
def training_status_api(request):
    """AJAX endpoint for fetching the live training status and progress.

    This function coordinates decentralized status synchronization by polling
    local Docker logs and mirroring state from other online peers in the swarm.

    Args:
        request (HttpRequest): The incoming request.

    Returns:
        JsonResponse: The job status payload.
    """
    current_network = SwarmNetwork.resolve_current(request.user)
    if not current_network: 
        logger.training.debug("StatusAPI: No current network found for user")
        return JsonResponse({"status": "no_network"})
    
    logger.training.debug(f"StatusAPI: Checking status for network {current_network.identifier} ({current_network.name})")
    
    # Identify server node and local state
    server_node = current_network.participants.filter(role="SERVER").first()
    from network.utils import get_tailscale_ip
    local_ip = get_tailscale_ip()
    is_server_node = server_node and server_node.ip == local_ip
    
    logger.training.debug(f"StatusAPI: local_ip={local_ip}, server_ip={server_node.ip if server_node else 'N/A'}, is_server_node={is_server_node}")
    
    job = TrainingJob.objects.filter(network=current_network).order_by("-created_at").first()
    if job:
        logger.training.debug(f"StatusAPI: Most recent job in DB: {job.flare_job_id} (status: {job.status})")
    
    # NEW: Scrape local docker logs for progress (User priority)
    local_participants = current_network.participants.filter(ip=local_ip)
    participant_ids = [p.participant_id for p in local_participants]
    
    logger.training.debug(f"StatusAPI: Local participant IDs: {participant_ids}")
    
    from .utils import scrape_docker_progress
    docker_results = scrape_docker_progress(participant_ids=participant_ids)
    
    if docker_results:
        logger.training.debug(f"StatusAPI: Docker scraping found {len(docker_results)} results: {docker_results}")
        for res in docker_results:
            merged_job = training_services.sync_job_from_docker_result(
                network=current_network, result=res
            )
            if merged_job and (not job or merged_job.created_at >= job.created_at):
                job = merged_job
    else:
        logger.training.debug("StatusAPI: No results from docker scraping")

    # 1. Mirror state from Peers (Server priority, then Clients)
    # This ensures decentralized sync: if PC2 starts training, PC3 can sync from PC2.
    
    mirror_targets = []
    
    # Target A: The Server Node
    effective_server_ip = server_node.ip if server_node and server_node.ip and server_node.ip != "-" else ""
    if not effective_server_ip:
        admin_target = _resolve_admin_session_target(current_network)
        if admin_target:
            _, _, effective_server_ip = admin_target
    
    if effective_server_ip and effective_server_ip != "-" and effective_server_ip != local_ip:
        mirror_targets.append(("SERVER", effective_server_ip))
        
    # Target B: Other Online Clients (Fallback)
    # We poll recent peers to find the active job initiator
    other_clients = current_network.participants.filter(role="CLIENT").exclude(ip__in=[local_ip, "-", ""]).order_by("-last_seen")[:3]
    for oc in other_clients:
        if oc.ip not in [t[1] for t in mirror_targets]:
            mirror_targets.append(("CLIENT", oc.ip))

    local_participant = current_network.participants.filter(ip=local_ip).order_by("role").first()
    
    # We use the network gossip token as the shared token for all internal P2P communication.
    # This allows nodes to trust each other without a central registry.
    gossip_token = current_network.gossip_token
    
    if local_participant and gossip_token:
        headers = {
            "X-Gossip-Participant": local_participant.participant_id,
            "X-Gossip-Token": gossip_token,
        }
    else:
        headers = {}
    remote_state_found = False

    for role, peer_ip in mirror_targets:
        if remote_state_found: break
        try:
            state_url = f"https://{peer_ip}:5085/training/api/state/{current_network.identifier}/"
            logger.training.debug(f"StatusAPI: Attempting to mirror state from {role} at {peer_ip}")
            
            # Short timeout per peer to keep the API responsive
            if not headers:
                continue
            resp = requests.get(
                state_url,
                headers=headers,
                timeout=2,
                verify=_peer_tls_verify_path(),
            )
            if resp.status_code == 200:
                remote_job = resp.json().get("job")
                if remote_job:
                    flare_id = remote_job["flare_job_id"]
                    logger.training.debug(f"StatusAPI: Received remote state for job {flare_id} from {peer_ip}")

                    merged_job = training_services.sync_job_from_remote_payload(
                        network=current_network, remote_job=remote_job
                    )
                    if merged_job and (not job or merged_job.created_at >= job.created_at):
                        job = merged_job

                    if merged_job and merged_job.status in {
                        "RUNNING",
                        "COMPLETED",
                        "FAILED",
                        "STOPPED",
                    }:
                        remote_state_found = True
        except Exception as e:
            logger.training.debug(f"StatusAPI: Failed to mirror state from {peer_ip}: {e}")

    nvflare_status = _nvflare_status_payload(current_network)
    if nvflare_status:
        logger.training.debug(f"StatusAPI: NVFlare admin API status: {nvflare_status}")
    
    if nvflare_status and nvflare_status.get("job_id"):
        mirror_job = training_services.sync_job_from_nvflare_status(
            network=current_network, nvflare_status=nvflare_status
        )
        if not job or (mirror_job and mirror_job.created_at >= job.created_at):
            job = mirror_job

    if not job:
        if nvflare_status: 
            logger.training.debug("StatusAPI: Returning NVFlare admin status as fallback")
            return JsonResponse(nvflare_status)
        logger.training.debug("StatusAPI: No job found, returning idle")
        return JsonResponse({"status": "idle"})

    payload = training_services.get_training_status_payload(
        network=current_network, job=job
    )
    logger.training.debug(f"StatusAPI: Final payload for {job.flare_job_id}: {payload}")
    
    # 2. Results Sync: Register results from local filesystem to local MinIO if COMPLETED
    if job.status == "COMPLETED":
        results_synced_key = f"results_synced_local_{job.identifier}"
        if not cache.get(results_synced_key):
            try:
                job_uuid = (
                    job.flare_job_uuid
                    or extract_flare_job_uuid(job.flare_job_id)
                    or str(job.flare_job_id)
                )
                
                # Path to local workspace where NVFlare produces results
                workspace_root = os.path.join(settings.BASE_DIR, "workspaces", str(job.project.identifier), str(current_network.identifier), "workspace")
                
                # List of possible result filenames produced by training
                possible_files = ["best_FL_model.pt", "model_weights.npz", "global_model.pt", "FL_model.pt"]
                
                found_and_synced = False
                from django.core.files.base import ContentFile
                from django.core.files.storage import default_storage

                for root, _dirs, files in os.walk(workspace_root):
                    if job_uuid in root:
                        for filename in files:
                            if filename in possible_files:
                                local_path = os.path.join(root, filename)
                                # Target key in local MinIO
                                s3_key = f"{job.project.identifier}/results/{job_uuid}/{filename}"
                                
                                if not default_storage.exists(s3_key):
                                    with open(local_path, "rb") as f:
                                        default_storage.save(s3_key, ContentFile(f.read()))
                                        logger.training.info(f"Registered local result to local MinIO: {s3_key}")
                                found_and_synced = True
                
                if found_and_synced:
                    cache.set(results_synced_key, True, 3600)
            except Exception as sync_err:
                logger.training.error(f"Failed to register local results to local MinIO: {sync_err}")

    if nvflare_status:
        local_status = str(payload.get("status", "")).upper().strip()
        nv_status = str(nvflare_status.get("status", "")).upper().strip()
        terminal_states = {"COMPLETED", "FAILED", "STOPPED"}
        unknown_states = {"", "UNKNOWN", "N/A", "NONE"}
        should_override_status = (
            nv_status not in unknown_states
            and not (local_status in terminal_states and nv_status not in terminal_states)
        )
        if should_override_status:
            payload["status"] = nvflare_status.get("status", payload["status"])
        payload["job_id"] = nvflare_status.get("job_id", payload["job_id"])
        nvflare_progress = nvflare_status.get("progress")
        if isinstance(nvflare_progress, (int, float)) and payload["progress"] < int(
            nvflare_progress
        ):
            payload["progress"] = int(nvflare_progress)
    return JsonResponse(payload)


@login_required
@project_context_required
def training_logs_api(request):
    """AJAX endpoint providing real-time log streaming for training jobs.

    Args:
        request (HttpRequest): The incoming request.

    Returns:
        JsonResponse: A JSON list of log lines formatted for display.
    """
    current_network = SwarmNetwork.resolve_current(request.user)
    if not current_network:
        return JsonResponse({"logs": []})
    logs = []
    try:
        job = (
            TrainingJob.objects.filter(network=current_network)
            .order_by("-created_at")
            .first()
        )
        if not job:
            return JsonResponse({"logs": logs})
        job_uuid = (
            job.flare_job_uuid
            or extract_flare_job_uuid(job.flare_job_id)
            or str(job.flare_job_id)
        )
        cache_key = f"training_log_path_{job_uuid}"
        latest_log = cache.get(cache_key)
        if latest_log and not os.path.exists(latest_log):
            latest_log = None
            cache.delete(cache_key)
        if not latest_log:
            latest_log = _find_latest_training_log(
                str(job.project.identifier),
                str(current_network.identifier),
                job_uuid,
            )
        if latest_log and os.path.exists(latest_log):
            with open(latest_log) as lf: lines = lf.readlines()[-100:]
            for line in lines:
                line = line.strip()
                if not line: continue
                ts, level, msg = "", "INFO", line
                ts_match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})", line)
                if ts_match:
                    ts = ts_match.group(1)
                    remaining = line[len(ts) :].strip(" -")
                    parts = remaining.split(" - ")
                    if len(parts) >= 2:
                        if parts[1] in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
                            level, msg = parts[1], " - ".join(parts[2:])
                        elif parts[0] in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
                            level, msg = parts[0], " - ".join(parts[1:])
                logs.append({"timestamp": ts, "level": level, "message": msg})
            logs.reverse()
    except Exception as e:
        logger.training.debug(f"Error in training_logs_api: {e}")
    return JsonResponse({"logs": logs})
