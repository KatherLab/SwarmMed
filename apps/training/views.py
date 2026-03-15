import ast
import json
import os
import re
import shutil
import socket
import ssl
import threading
import time
import requests

from common.utils import get_safe_slug
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from logs.logger import get_logger
from network.models import SwarmNetwork, UserCurrentNetwork
from project.decorators import (
    project_context_required,
    project_membership_required,
)
from project.models import UserCurrentProject

from .models import TrainingJob
from .utils import download_s3_folder

logger = get_logger()


@login_required
def training_api_state(request, network_id):
    """
    Internal API: Returns the latest training job state from this node.
    Used by client nodes to mirror the server node's training state.
    """
    network = get_object_or_404(SwarmNetwork, identifier=network_id)
    job = TrainingJob.objects.filter(network=network).order_by("-created_at").first()
    
    if not job:
        return JsonResponse({"job": None})
        
    return JsonResponse({
        "job": {
            "flare_job_id": job.flare_job_id,
            "status": job.status,
            "total_rounds": job.total_rounds,
            "rounds_finished": job.rounds_finished,
            "progress_percent": job.progress_percent,
            "created_at": job.created_at.isoformat(),
        }
    })


@login_required
def training_api_results(request, network_id):
    """
    Internal API: Provides the training results (aggregated model) as a download.
    Server node serves this to client nodes for decentralized sync.
    """
    network = get_object_or_404(SwarmNetwork, identifier=network_id)
    job = TrainingJob.objects.filter(network=network, status="COMPLETED").order_by("-created_at").first()
    
    if not job:
        return JsonResponse({"error": "No completed job found"}, status=404)
        
    # Logic to find the aggregated model file in the workspace
    job_uuid = str(job.flare_job_id)
    match = re.search(r"([0-9a-f-]{36})", job_uuid)
    if match: job_uuid = match.group(1)
    
    workspace_root = os.path.join(settings.BASE_DIR, "workspaces", str(job.project.identifier), str(network.identifier), "workspace")
    model_path = None
    
    # Heuristic to find the best model file
    for root, dirs, files in os.walk(workspace_root):
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
    if not job:
        return {
            "status": "idle",
            "progress": 0,
            "duration": "-",
            "eta": "-",
            "job_id": None,
            "created_at": timezone.now().isoformat(),
        }

    info = get_training_progress_info(job, current_network)
    return {
        "status": info["status"],
        "progress": info["progress"],
        "duration": info["duration"],
        "eta": info["eta"],
        "job_id": job.flare_job_id,
        "created_at": timezone.now().isoformat(),
    }


def _resolve_admin_startup_dir(current_network) -> str | None:
    if current_network.admin_startup_dir and os.path.exists(
        current_network.admin_startup_dir
    ):
        return current_network.admin_startup_dir

    override = os.environ.get("SWARMCLOUD_NVFLARE_ADMIN_DIR", "").strip()
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
    startup_dir = _resolve_admin_startup_dir(current_network)
    if not startup_dir:
        return None

    startup_dir = os.path.abspath(startup_dir)
    
    # NVFlare expects a directory that contains a 'startup' subdirectory 
    # which in turn contains 'fed_admin.json'.
    
    session_dir = startup_dir
    if os.path.basename(startup_dir.rstrip(os.sep)) == "startup":
        session_dir = os.path.dirname(startup_dir.rstrip(os.sep))
    else:
        if os.path.exists(os.path.join(startup_dir, "fed_admin.json")):
            session_dir = os.path.dirname(startup_dir.rstrip(os.sep))

    candidates = [
        session_dir,
        startup_dir,
        os.path.dirname(startup_dir.rstrip(os.sep)),
    ]
    canonical = ""
    for cand in candidates:
        if not cand:
            continue
        cand = os.path.abspath(cand)
        if os.path.exists(os.path.join(cand, "startup", "fed_admin.json")):
            canonical = cand
            break
        if os.path.exists(os.path.join(cand, "fed_admin.json")):
            if os.path.basename(cand.rstrip(os.sep)) == "startup":
                canonical = os.path.dirname(cand.rstrip(os.sep))
            else:
                canonical = cand
            break
        if os.path.exists(os.path.join(cand, "startup", "startup", "fed_admin.json")):
            canonical = os.path.join(cand, "startup")
            break

    if canonical:
        session_dir = canonical

    for folder in ["local", "transfer", "logs"]:
        try:
            os.makedirs(os.path.join(session_dir, folder), exist_ok=True)
        except Exception:
            pass
    
    admin_name = "admin@nvidia.com"
    admin_cert_path = os.path.join(session_dir, "startup", "client.crt")
    cert_cn = _extract_cert_common_name(admin_cert_path)
    if cert_cn:
        admin_name = cert_cn
    
    if not cert_cn:
        dir_name = os.path.basename(session_dir.rstrip(os.sep))
        if "@" in dir_name:
            admin_name = dir_name
        else:
            try:
                project_name = get_safe_slug(
                    current_network.project.title, current_network.project.identifier
                ).replace("-", "_")
                client_cfgs = [
                    os.path.join("workspaces", str(current_network.project.identifier), str(current_network.identifier), "workspace", project_name, "prod_00", "startup", "fed_client.json"),
                    os.path.join("workspaces", str(current_network.project.identifier), str(current_network.identifier), "workspace", "prod_00", "startup", "fed_client.json"),
                    os.path.join(session_dir, "startup", "fed_client.json")
                ]
                for client_cfg in client_cfgs:
                    if os.path.exists(client_cfg):
                        with open(client_cfg) as f:
                            data = json.load(f)
                            c_name = data.get("client_name")
                            if c_name and c_name != "server":
                                admin_name = f"admin-{c_name}@nvidia.com"
                                break
            except Exception:
                pass

    server_ip = ""
    try:
        env_host = os.getenv("SWARMCLOUD_SERVER_HOST", "").strip()
        if env_host:
            server_ip = env_host

        host_file = os.path.join(session_dir, "startup", "server_host.txt")
        if not server_ip and os.path.exists(host_file):
            server_ip = open(host_file).read().strip()
        else:
            prod_00 = os.path.dirname(session_dir.rstrip(os.sep))
            sibling_host = os.path.join(prod_00, "startup", "server_host.txt")
            if not server_ip and os.path.exists(sibling_host):
                server_ip = open(sibling_host).read().strip()
            else:
                client_cfg = os.path.join(prod_00, "startup", "fed_client.json")
                if not server_ip and os.path.exists(client_cfg):
                    from network.tasks import _extract_host_from_server_endpoint
                    extracted = _extract_host_from_server_endpoint(os.path.dirname(client_cfg))
                    if extracted:
                        server_ip = extracted
    except Exception:
        pass

    return (admin_name, session_dir, server_ip)


def _parse_nvflare_jobs(response):
    if isinstance(response, dict):
        if "jobs" in response and isinstance(response["jobs"], list):
            return response["jobs"]
        if "data" in response and isinstance(response["data"], list):
            return response["data"]
        if "job_id" in response:
            return [response]
    if isinstance(response, list):
        return response

    text = str(response or "")
    jobs = []
    uuid_re = re.compile(r"([0-9a-f-]{36})")
    status_re = re.compile(r"\b(RUNNING|COMPLETED|FAILED|STOPPED)\b", re.I)
    for line in text.splitlines():
        uid_match = uuid_re.search(line)
        status_match = status_re.search(line)
        if uid_match:
            jobs.append(
                {
                    "job_id": uid_match.group(1),
                    "status": status_match.group(1).upper()
                    if status_match
                    else "UNKNOWN",
                }
            )
    return jobs


def _select_nvflare_job(jobs):
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
    deduped = []
    for value in values:
        if value in deduped:
            continue
        deduped.append(value)
    return deduped


_NVFLARE_GRPC_PATCHED: bool = False
_NVFLARE_GRPC_PATCH_LOCK = threading.Lock()

import logging as _stdlib_logging
_grpc_patch_log = _stdlib_logging.getLogger(__name__)


def _ensure_grpc_ssl_patched(tls_server_name: str) -> None:
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
    requested_host = (requested_host or "").strip()
    default_host = (default_host or "").strip()
    seed_candidates = ["server", requested_host, default_host, "127.0.0.1", "localhost", ""]
    return _dedupe_keep_order([c for c in seed_candidates if c is not None])


def _build_flare_port_candidates(default_port: int = 0):
    env_admin_port = os.getenv("SWARMCLOUD_FLARE_ADMIN_PORT", "").strip()
    candidates = []
    if env_admin_port:
        try:
            candidates.append(int(env_admin_port))
        except ValueError:
            pass
    if default_port and int(default_port) > 0 and int(default_port) not in candidates:
        candidates.append(int(default_port))
    if 8003 not in candidates:
        candidates.append(8003)
    if 8002 not in candidates:
        candidates.append(8002)
    return candidates


def _log_flare_pre_submit_diagnostics(log, username: str, startup_kit_location: str, requested_host: str):
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
                    with socket.create_connection((effective_host, int(port)), timeout=2.0) as s:
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
    """Extract list of client names from FLARE list_clients response."""
    clients = []
    if isinstance(response, dict):
        data = response.get("data") or response.get("clients") or []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("client_name")
                    if name: clients.append(str(name))
                elif isinstance(item, str):
                    clients.append(item)
    if not clients:
        text = str(response or "")
        for line in text.splitlines():
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 2:
                name = parts[0]
                if name.lower() not in ["client name", "name", "-------", "server", "admin"]:
                    clients.append(name)
    return _dedupe_keep_order(clients)


def new_secure_session_with_host(username: str, startup_kit_location: str, host: str, debug: bool = False, timeout: float = 20.0, network_id=None):
    from nvflare.fuel.flare_api.flare_api import Session
    canonical_host = ""
    admin_port = 0
    try:
        temp_session = Session(
            username=username,
            startup_path=startup_kit_location,
            secure_mode=True,
        )
        if temp_session.api:
            canonical_host = str(getattr(temp_session.api, "host", "") or "").strip()
            try:
                admin_port = int(getattr(temp_session.api, "port", 0) or 0)
            except Exception:
                admin_port = 0
        
        if temp_session.api and not getattr(temp_session.api, "cell", None):
            temp_session.api.closed = True
        else:
            try:
                temp_session.close()
            except Exception:
                pass
    except Exception:
        pass

    canonical_host = canonical_host or "server"
    admin_port = admin_port if admin_port > 0 else 8003

    requested_host = (host or "").strip()
    ip_candidates = [canonical_host, requested_host]
    
    if network_id:
        short_id = str(network_id)[:12]
        ip_candidates.append(f"swarm-{short_id}-server")
    
    ip_candidates.extend(["172.17.0.1", "host.docker.internal", "127.0.0.1", "localhost"])
    ip_candidates = _dedupe_keep_order([c for c in ip_candidates if c])

    _ensure_grpc_ssl_patched(canonical_host)

    reachable: set = set()
    for _ip in ip_candidates:
        try:
            with socket.create_connection((_ip, admin_port), timeout=1.0):
                reachable.add(_ip)
        except Exception:
            pass

    connection_errors = []

    for candidate in ip_candidates:
        connect_timeout = timeout if candidate in reachable else min(timeout, 5.0)
        session = Session(
            username=username,
            startup_path=startup_kit_location,
            secure_mode=True,
            debug=debug,
        )

        try:
            if session.api:
                try:
                    session.api.authenticate_msg_timeout = max(
                        float(timeout),
                        float(getattr(session.api, "authenticate_msg_timeout", 5.0) or 5.0),
                    )
                except Exception:
                    pass

                session.api.host = candidate
                session.api.port = int(admin_port)

                try:
                    session.try_connect(connect_timeout)
                    _cell_deadline = time.monotonic() + min(connect_timeout, 8.0)
                    while (
                        getattr(session.api, "cell", None) is None
                        and time.monotonic() < _cell_deadline
                    ):
                        time.sleep(0.15)

                    if getattr(session.api, "cell", None) is not None:
                        time.sleep(2.0)

                    submit_cmd_info = None
                    try:
                        submit_cmd_info = session.api.check_command("submit_job")
                    except Exception as cmd_probe_error:
                        connection_errors.append(
                            f"host={candidate} port={admin_port}: "
                            f"connected but submit command probe failed: {cmd_probe_error}"
                        )
                        if session.api and getattr(session.api, "cell", None):
                            session.close()
                        else:
                            session.api.closed = True
                        continue

                    if getattr(submit_cmd_info, "name", "") in {"UNKNOWN", "AMBIGUOUS"}:
                        connection_errors.append(
                            f"host={candidate} port={admin_port}: "
                            f"connected but submit_job unavailable ({submit_cmd_info})"
                        )
                        if session.api and getattr(session.api, "cell", None):
                            session.close()
                        else:
                            session.api.closed = True
                        continue

                    return session

                except Exception as e:
                    connection_errors.append(
                        f"host={candidate} port={admin_port}: {e}"
                    )
            else:
                session.try_connect(connect_timeout)
                _cell_deadline = time.monotonic() + min(connect_timeout, 8.0)
                while (
                    getattr(session.api, "cell", None) is None
                    and time.monotonic() < _cell_deadline
                ):
                    time.sleep(0.15)
                if getattr(session.api, "cell", None) is not None:
                    time.sleep(2.0)
                return session

        except Exception as e:
            connection_errors.append(
                f"host={candidate} port={admin_port}: {e}"
            )

        try:
            if session.api and getattr(session.api, "cell", None):
                session.close()
            elif session.api:
                session.api.closed = True
        except Exception:
            pass

    if connection_errors:
        raise RuntimeError(
            "cannot connect to FLARE admin API. Attempts: "
            + " | ".join(connection_errors[:10])
        )
    raise RuntimeError("cannot connect to FLARE admin API")


def _nvflare_status_payload(current_network):
    cache_key = f"nvflare_status_payload_{current_network.identifier}"
    cached_payload = cache.get(cache_key)
    if cached_payload is not None:
        return cached_payload

    admin_target = _resolve_admin_session_target(current_network)
    if not admin_target:
        cache.set(cache_key, None, 15)
        return None

    try:
        admin_name, admin_dir, server_ip = admin_target
        sess = new_secure_session_with_host(
            username=admin_name, 
            startup_kit_location=admin_dir,
            host=server_ip,
            timeout=5.0,
            network_id=current_network.identifier
        )
        response = sess.api.do_command("list_jobs")
        try:
            sess.close()
        except Exception:
            pass

        jobs = _parse_nvflare_jobs(response)
        job = _select_nvflare_job(jobs)
        if not job:
            cache.set(cache_key, None, 15)
            return None

        status = (
            str(
                job.get("status")
                or job.get("job_status")
                or job.get("state")
                or "UNKNOWN"
            )
            .upper()
            .strip()
        )
        job_id = job.get("job_id") or job.get("id") or "unknown"

        if status in {"", "UNKNOWN", "N/A", "NONE"}:
            cache.set(cache_key, None, 15)
            return None

        progress = 0
        if status in {"COMPLETED", "STOPPED"}:
            progress = 100

        payload = {
            "status": status.title(),
            "progress": progress,
            "duration": "-",
            "eta": "-",
            "job_id": job_id,
            "created_at": timezone.now().isoformat(),
        }
        cache.set(cache_key, payload, 15)
        return payload
    except Exception:
        cache.set(cache_key, None, 15)
        return None


def _tail_text(file_path: str, max_bytes: int = 2048 * 1024) -> str:
    try:
        with open(file_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            start = max(0, end - max_bytes)
            f.seek(start)
            data = f.read()
        return data.decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _find_latest_training_log(project_id: str, network_id: str, job_uuid: str, cache_ttl_seconds: int = 60) -> str | None:
    cache_key = f"training_log_path_{project_id}_{network_id}_{job_uuid}"
    cached_path = cache.get(cache_key)
    if cached_path and os.path.exists(cached_path):
        return cached_path

    workspace_root = os.path.join("workspaces", project_id, network_id, "workspace")
    if not os.path.exists(workspace_root):
        return None

    latest_log = None
    for root, dirs, files in os.walk(workspace_root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        if job_uuid in root:
            for cand in ("log_fl.txt", "log.txt"):
                if cand in files:
                    latest_log = os.path.join(root, cand)
                    break
        if latest_log:
            break

    if latest_log and os.path.exists(latest_log):
        cache.set(cache_key, latest_log, cache_ttl_seconds)
        return latest_log
    return None


def get_user_project(request):
    try:
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        if not user_current_project.project:
            return None, False
        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False


def format_duration(seconds):
    if seconds < 0: return "0s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0: return f"{h}h {m}m {s}s"
    if m > 0: return f"{m}m {s}s"
    return f"{s}s"


def get_training_progress_info(training_job, current_network):
    cache_key = f"training_progress_{training_job.id}_{training_job.status}"
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        return cached_result

    training_progress = 0
    training_status = training_job.status.title()
    is_training_running = False
    duration_str = "-"
    eta_str = "-"

    job_uuid = str(training_job.flare_job_id)
    match = re.search(r"Submitted job:\s*([0-9a-f-]+)", job_uuid)
    if match: job_uuid = match.group(1)
    else:
        match_uuid = re.search(r"([0-9a-f-]{36})", job_uuid)
        if match_uuid: job_uuid = match_uuid.group(1)

    if training_job.status == "RUNNING":
        is_training_running = True
        have_cached_progress = False
        try:
            if training_job.progress_updated_at:
                age = (timezone.now() - training_job.progress_updated_at).total_seconds()
                if age <= 60 and training_job.progress_percent is not None:
                    training_progress = int(training_job.progress_percent)
                    if training_progress >= 100: training_progress = 99
                    have_cached_progress = True
        except (TypeError, ValueError):
            have_cached_progress = False

        try:
            total_rounds = 10
            # Be more aggressive about finding the server config
            workspace_dir = os.path.join("workspaces", str(training_job.project.identifier), str(current_network.identifier), "workspace")
            for root, dirs, files in os.walk(workspace_dir):
                if "config_fed_server.json" in files:
                    try:
                        with open(os.path.join(root, "config_fed_server.json")) as f:
                            cfg = json.load(f)
                            for workflow in cfg.get("workflows", []):
                                if workflow.get("id") == "swarm_controller":
                                    total_rounds = int(workflow.get("args", {}).get("num_rounds", 10))
                                    break
                    except Exception: pass
                if total_rounds != 10: break

            rounds_finished = 0
            ended = False
            if have_cached_progress:
                rounds_finished = training_job.rounds_finished or 0

            # Robust log scanning regexes
            round_patterns = [
                re.compile(r"Finished round\s+(\d+)", re.I),
                re.compile(r"Round\s+(\d+)\s+\|", re.I),
                re.compile(r"Round:\s+(\d+)", re.I),
                re.compile(r"finished training round\s+(\d+)", re.I),
                re.compile(r"number of rounds completed\s+(\d+)", re.I),
                re.compile(r"Start aggregation for round\s+(\d+)", re.I),
            ]

            def scan_log_tail(fpath: str) -> None:
                nonlocal ended, rounds_finished
                data = _tail_text(fpath, max_bytes=512 * 1024)
                if not data: return
                
                completion_markers = ["ending workflow", "child worker process finished", "MPM: Good Bye!", "training finished", "job finished", "Swarm Learning Done"]
                if any(m.lower() in data.lower() for m in completion_markers):
                    ended = True
                
                for pattern in round_patterns:
                    for m in pattern.finditer(data):
                        rnum = int(m.group(1))
                        if rnum > rounds_finished: rounds_finished = rnum

            if not have_cached_progress:
                for root, dirs, files in os.walk(workspace_dir):
                    if job_uuid in root:
                        for fname in files:
                            if fname.startswith("log") and fname.endswith(".txt"):
                                scan_log_tail(os.path.join(root, fname))
                                if ended: break
                    if ended: break

            if ended:
                training_progress = 100
                training_status = "Completed"
                is_training_running = False
                if training_job.status != "COMPLETED":
                    training_job.status = "COMPLETED"
                    training_job.completed_at = timezone.now()
                    training_job.save(update_fields=["status", "completed_at"])
            elif not have_cached_progress and total_rounds > 0:
                rounds_completed = rounds_finished + 1 if rounds_finished >= 0 else 0
                training_progress = min(99, int(rounds_completed * 100 / total_rounds))
        except Exception as e:
            logger.training.debug(f"Failed to calculate training progress: {e}")
            training_progress = 0

    elif training_job.status == "COMPLETED":
        training_progress = 100
        training_status = "Completed"

    now = timezone.now()
    start_time = training_job.created_at
    if training_job.status == "RUNNING":
        elapsed = (now - start_time).total_seconds()
        duration_str = format_duration(elapsed)
        if training_progress > 0:
            total_est = elapsed / (training_progress / 100.0)
            remaining = total_est - elapsed
            eta_str = format_duration(remaining)
        else:
            eta_str = "Calculating..."
    elif training_job.completed_at:
        elapsed = (training_job.completed_at - start_time).total_seconds()
        duration_str = format_duration(elapsed)
        eta_str = "Finished"

    result = {"status": training_status, "progress": training_progress, "duration": duration_str, "eta": eta_str, "is_running": is_training_running}
    if training_job.status == "RUNNING":
        cache.set(cache_key, result, 5)
    return result


@login_required
@project_context_required
def training(request):
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
                job_uuid = str(training_job.flare_job_id)
                match = re.search(r"Submitted job:\s*([0-9a-f-]+)", job_uuid)
                if match: job_uuid = match.group(1)
                else:
                    match_uuid = re.search(r"([0-9a-f-]{36})", job_uuid)
                    if match_uuid: job_uuid = match_uuid.group(1)
                latest_log = _find_latest_training_log(str(training_job.project.identifier), str(current_network.identifier), job_uuid)
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
    network = get_object_or_404(SwarmNetwork, identifier=network_id)
    if network.status != "RUNNING":
        messages.error(request, f"Network '{network.name}' is not running. Please start the network before initiating training.")
        return redirect("training:training")

    project = network.project
    log = get_logger(user=request.user, project=project)
    job_dir = os.path.join(settings.BASE_DIR, "workspaces", str(project.identifier), str(network.identifier), "job")
    project_name = get_safe_slug(project.title, project.identifier).replace("-", "_")
    admin_target = _resolve_admin_session_target(network)
    if not admin_target:
        messages.error(request, "No admin startup kit found for this center. Please re-provision or upload a complete startup package.")
        return redirect("training:training")

    admin_username, admin_session_dir, server_ip = admin_target
    app_server_dir = os.path.join(job_dir, "app_server")
    app_client_dir = os.path.join(job_dir, "app_client")
    app_client_custom_dir = os.path.join(app_client_dir, "custom")
    os.makedirs(os.path.join(app_server_dir, "config"), exist_ok=True)
    os.makedirs(os.path.join(app_client_dir, "config"), exist_ok=True)
    os.makedirs(app_client_custom_dir, exist_ok=True)

    source_code_prefix = f"{project.identifier}/code/training/"
    try:
        download_s3_folder(settings.AWS_STORAGE_BUCKET_NAME, source_code_prefix, app_client_custom_dir)
        log.training.info(f"Downloaded training code from S3: {source_code_prefix}")
    except Exception as e:
        log.training.error(f"Failed to download training code: {e}")

    flare_adapter_src = os.path.join(settings.BASE_DIR, "apps", "training", "flare_adapter.py")
    shutil.copyfile(flare_adapter_src, os.path.join(app_client_custom_dir, "flare_adapter.py"))

    # We NO LONGER build a data manifest on the coordinator.
    # Each node now dynamically discovers its own local data at runtime using 
    # the flare_adapter's local discovery logic. This prevents sensitive 
    # data URLs from being leaked across the swarm.

    training_py_path = os.path.join(app_client_custom_dir, "training.py")
    if os.path.exists(training_py_path):
        with open(training_py_path) as f: content = f.read()
        replacement = f'main(project_id="{str(project.identifier)}")'
        content = content.replace('main(project_id="default_project")', replacement).replace("main(project_id='default_project')", replacement)
        with open(training_py_path, "w") as f: f.write(content)

    client_names = list(network.participants.filter(role="CLIENT").values_list("participant_id", flat=True))
    if not client_names:
        _workspace_root = os.path.join(settings.BASE_DIR, "workspaces", str(project.identifier), str(network.identifier))
        try:
            _prod_00_check = os.path.dirname(os.path.abspath(admin_session_dir))
            _lcn_file = os.path.join(_prod_00_check, ".local_client_names.json")
            if os.path.exists(_lcn_file):
                with open(_lcn_file) as _f:
                    _lcn = json.load(_f)
                if isinstance(_lcn, list): client_names.extend([str(n) for n in _lcn if n])
            
            if not client_names:
                _ap_file = os.path.join(admin_session_dir, "startup", ".all_participants.json")
                if os.path.exists(_ap_file):
                    with open(_ap_file) as _f:
                        _ap = json.load(_f)
                    if isinstance(_ap, dict) and "clients" in _ap:
                        client_names.extend([str(n) for n in _ap["clients"] if n])
        except Exception: pass

        if not client_names:
            try:
                import yaml as _yaml
                _project_yml = os.path.join(_workspace_root, "project.yml")
                if os.path.exists(_project_yml):
                    with open(_project_yml) as _f:
                        _yml = _yaml.safe_load(_f) or {}
                    for _p in _yml.get("participants", []):
                        _role = str(_p.get("type", _p.get("role", ""))).lower()
                        _name = str(_p.get("name", "")).strip()
                        if _name and _role in {"client", "fl_client"}: client_names.append(_name)
            except Exception: pass

        if not client_names:
            try:
                _prod_00 = os.path.dirname(os.path.abspath(admin_session_dir))
                _EXCL = {"server", "admin_startup", "startup", "transfer", "local", "logs", "custom"}
                for _e in sorted(os.scandir(_prod_00), key=lambda x: x.name):
                    if (_e.is_dir() and _e.name not in _EXCL and not _e.name.startswith(".") and os.path.isdir(os.path.join(_e.path, "startup"))):
                        client_names.append(_e.name)
            except Exception: pass

    # Sanitize current list (dedupe and filter)
    client_names = _dedupe_keep_order([n for n in client_names if n and n.lower() not in ["server", "admin"]])

    server_names = list(network.participants.filter(role="SERVER").values_list("participant_id", flat=True))
    if "server" not in server_names: server_names = ["server"]

    framework = "np"
    if os.path.exists(training_py_path):
        with open(training_py_path) as f: script_text = f.read()
        try:
            tree = ast.parse(script_text)
            imported_modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        mod = (alias.name or "").split(".")[0].strip()
                        if mod: imported_modules.add(mod)
                elif isinstance(node, ast.ImportFrom):
                    mod = (node.module or "").split(".")[0].strip()
                    if mod: imported_modules.add(mod)
            if imported_modules.intersection({"tensorflow", "keras"}): framework = "tf"
            elif imported_modules.intersection({"torch", "pytorch_lightning", "lightning", "transformers", "monai"}): framework = "pt"
            elif imported_modules.intersection({"sklearn", "numpy", "pandas"}): framework = "np"
            elif "sklearn" in imported_modules: framework = "np"
        except SyntaxError:
            if "tensorflow" in script_text.lower() or "keras" in script_text.lower(): framework = "tf"

    log.training.info(f"Detected training framework: {framework}")
    if framework == "pt":
        runtime_requirements_path = os.path.join(settings.BASE_DIR, "workspaces", str(project.identifier), str(network.identifier), "runtime_requirements.txt")
        has_torch_dependency = False
        if os.path.exists(runtime_requirements_path):
            try:
                with open(runtime_requirements_path) as rf:
                    for raw_line in rf:
                        line = raw_line.strip().lower()
                        if not line or line.startswith("#"): continue
                        if line.startswith("torch"):
                            has_torch_dependency = True
                            break
            except Exception as e: log.training.warning(f"Could not validate runtime requirements for PyTorch: {e}")
        if not has_torch_dependency:
            err_msg = "PyTorch training detected, but runtime requirements do not include 'torch'. Add torch to the project's requirements file, re-provision/start the network, and retry training."
            log.training.error(err_msg)
            messages.error(request, err_msg)
            return redirect("training:training")

    try:
        submit_connect_timeout = 20.0
        env_timeout = os.getenv("SWARMCLOUD_FLARE_CONNECT_TIMEOUT", "").strip()
        if env_timeout:
            try: submit_connect_timeout = float(env_timeout)
            except ValueError: pass

        log.training.info(f"FLARE submit timeout config: requested_timeout={submit_connect_timeout}, env_SWARMCLOUD_FLARE_CONNECT_TIMEOUT={env_timeout or 'unset'}")

        from nvflare.job_config.api import FedJob
        from nvflare.app_common.ccwf import SwarmServerController, SwarmClientController
        from nvflare.apis.dxo import DataKind
        from nvflare.app_common.aggregators.intime_accumulate_model_aggregator import InTimeAccumulateWeightedAggregator
        from nvflare.app_common.ccwf.comps.simple_model_shareable_generator import SimpleModelShareableGenerator
        from nvflare.app_common.ccwf.comps.simple_intime_model_selector import SimpleIntimeModelSelector

        # Set default executor
        from nvflare.app_common.executors.in_process_client_api_executor import InProcessClientAPIExecutor
        executor = InProcessClientAPIExecutor(task_script_path="custom/training.py")

        # Select appropriate persistor based on framework
        # NOTE: PTFileModelPersistor is used for PT and TF because it handles dictionaries of arrays.
        if framework == "np":
            try:
                from nvflare.app_common.np.np_model_persistor import NPModelPersistor
                persistor = NPModelPersistor()
            except (ModuleNotFoundError, ImportError):
                from nvflare.app_opt.pt.file_model_persistor import PTFileModelPersistor
                persistor = PTFileModelPersistor()
        else:
            try:
                from nvflare.app_opt.pt.file_model_persistor import PTFileModelPersistor
                persistor = PTFileModelPersistor()
            except (ModuleNotFoundError, ImportError):
                from nvflare.app_common.np.np_model_persistor import NPModelPersistor
                persistor = NPModelPersistor()

        if framework == "tf":
            try:
                from nvflare.app_opt.tf.in_process_client_api_executor import TFInProcessClientAPIExecutor
                executor = TFInProcessClientAPIExecutor(task_script_path="custom/training.py")
            except (ModuleNotFoundError, ImportError):
                pass
        elif framework == "pt":
            use_pt_executor = os.getenv("SWARMCLOUD_ENABLE_PT_EXECUTOR", "").strip().lower() in {"1", "true", "yes", "on"}
            if use_pt_executor:
                try:
                    from nvflare.app_opt.pt.in_process_client_api_executor import PTInProcessClientAPIExecutor
                    executor = PTInProcessClientAPIExecutor(task_script_path="custom/training.py")
                except (ModuleNotFoundError, ImportError):
                    pass

        # Select appropriate shareable generator
        shareable_generator = SimpleModelShareableGenerator()

        # Select appropriate aggregator
        aggregator = InTimeAccumulateWeightedAggregator(expected_data_kind=DataKind.WEIGHTS)
        
        # Add model selector for tracking best model
        model_selector = SimpleIntimeModelSelector(validation_metric_name="accuracy")
        
        log.training.info(f"Selected NVFlare executor: {executor.__class__.__module__}.{executor.__class__.__name__}")

        _log_flare_pre_submit_diagnostics(log=log, username=admin_username, startup_kit_location=admin_session_dir, requested_host=server_ip)

        sess = new_secure_session_with_host(username=admin_username, startup_kit_location=admin_session_dir, host=server_ip, timeout=submit_connect_timeout, network_id=network.identifier)

        # Dynamic client discovery if local methods yielded nothing or generic defaults
        if not client_names or set(client_names).issubset({"fl-client-1", "fl-client-2"}):
            try:
                log.training.info("Querying live server for connected clients...")
                client_resp = sess.api.do_command("list_clients")
                live_clients = _parse_nvflare_clients(client_resp)
                if live_clients:
                    client_names = live_clients
                    log.training.info(f"Discovered connected clients: {client_names}")
            except Exception as _ce:
                log.training.warning(f"Live client discovery failed: {_ce}")

        if not client_names: client_names = ["fl-client-1", "fl-client-2"]

        job = FedJob(name=f"{project_name}_job")
        private_p2p = os.getenv("SWARMCLOUD_PRIVATE_P2P", "").strip().lower() in {"1", "true", "yes", "on"}
        starting_client = client_names[0] if client_names else ""

        # Try to extract the number of swarm rounds from the training script
        swarm_rounds = 10
        try:
            training_script_path = os.path.join(app_client_custom_dir, "training.py")
            if os.path.exists(training_script_path):
                with open(training_script_path, "r") as f:
                    content = f.read()
                    match = re.search(r"SWARM_ROUNDS\s*=\s*(\d+)", content)
                    if match:
                        swarm_rounds = int(match.group(1))
                        log.training.info(f"Extracted SWARM_ROUNDS={swarm_rounds} from training script: {training_script_path}")
                    else:
                        log.training.warning(f"SWARM_ROUNDS not found in {training_script_path}, defaulting to 10")
            else:
                log.training.warning(f"Training script not found at {training_script_path} for round extraction, defaulting to 10")
        except Exception as e:
            log.training.warning(f"Failed to extract SWARM_ROUNDS from training script: {e}")

        controller = SwarmServerController(
            num_rounds=swarm_rounds, participating_clients=client_names, result_clients=client_names,
            starting_client=starting_client, private_p2p=private_p2p, aggr_clients=client_names, train_clients=client_names,
        )
        log.training.info(f"Swarm controller config: private_p2p={private_p2p}, starting_client={starting_client}, participants={client_names}")
        for server_name in server_names:
            job.to(controller, server_name)
            job.to(persistor, server_name, id="persistor")
            job.to(shareable_generator, server_name, id="shareable_generator")
            job.to(aggregator, server_name, id="aggregator")

        swarm_client_controller = SwarmClientController(
            learn_task_name="train", persistor_id="persistor", aggregator_id="aggregator",
            shareable_generator_id="shareable_generator", min_responses_required=len(client_names),
        )

        for client_name in client_names:
            job.to(executor, client_name, tasks=["train", "validate", "submit_model"])
            job.to(swarm_client_controller, client_name, tasks=["swarm_*"])
            job.to(persistor, client_name, id="persistor")
            job.to(shareable_generator, client_name, id="shareable_generator")
            job.to(aggregator, client_name, id="aggregator")
            job.to(model_selector, client_name, id="model_selector")
            job.to(app_client_custom_dir, client_name)

        generated_jobs_root = os.path.join(job_dir, "generated")
        os.makedirs(generated_jobs_root, exist_ok=True)
        job.export_job(generated_jobs_root)
        job_definition_path = os.path.abspath(os.path.join(generated_jobs_root, job.name))
        log.training.info(f"Submitting exported job from: {job_definition_path}")

        job_id = sess.submit_job(job_definition_path)
        try: sess.close()
        except Exception: pass

        TrainingJob.objects.create(project=project, network=network, status="RUNNING" if job_id else "FAILED", flare_job_id=job_id or "unknown")
        messages.success(request, f"Successfully submitted job {job_id}")
    except Exception as e:
        log.training.error(f"Submit job via FLARE API failed: {e}")
        TrainingJob.objects.create(project=project, network=network, status="FAILED", flare_job_id="error")
        messages.error(request, f"Failed to submit job: {e}")

    return redirect("training:training")


@login_required
@project_membership_required
def stop_training(request, network_id):
    network = get_object_or_404(SwarmNetwork, identifier=network_id)
    job = TrainingJob.objects.filter(network=network, status="RUNNING").order_by("-created_at").first()
    if not job:
        messages.warning(request, "No running job found to stop.")
        return redirect("training:training")

    try:
        admin_target = _resolve_admin_session_target(network)
        if not admin_target:
            messages.error(request, "No admin startup kit found for this center.")
            return redirect("training:training")

        admin_username, admin_user_dir, server_ip = admin_target
        sess = new_secure_session_with_host(username=admin_username, startup_kit_location=admin_user_dir, host=server_ip, network_id=network.identifier)
        job_uuid = str(job.flare_job_id)
        match = re.search(r"([0-9a-f-]{36})", job_uuid)
        if match: job_uuid = match.group(1)
        sess.api.do_command(f"abort_job {job_uuid}")
        job.status = "STOPPED"
        job.completed_at = timezone.now()
        job.progress_percent = 100
        job.progress_updated_at = timezone.now()
        job.save(update_fields=["status", "completed_at", "progress_percent", "progress_updated_at"])
        messages.success(request, f"Successfully aborted job {job_uuid}")
    except Exception as e:
        logger.training.error(f"Abort job failed: {e}")
        err = str(e).lower()
        if any(token in err for token in ["not running", "invalid job id", "no such job"]):
            job.status = "COMPLETED"
            job.completed_at = timezone.now()
            job.progress_percent = 100
            job.progress_updated_at = timezone.now()
            job.save(update_fields=["status", "completed_at", "progress_percent", "progress_updated_at"])
            messages.info(request, "Training job had already finished. Status updated to Completed.")
        else: messages.error(request, f"Failed to abort job: {e}")
    return redirect("training:training")


@login_required
@project_context_required
def training_status_api(request):
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
            job_id = res["job_id"]
            # If we can't find a job ID in logs, try to match with the most recent job in DB
            if not job_id and job:
                job_id = job.flare_job_id
            
            if job_id:
                logger.training.debug(f"StatusAPI: Processing docker result for job_id {job_id} (rounds: {res['rounds_finished']}, ended: {res['ended']})")
                # Find or create mirror job in local database
                l_job = TrainingJob.objects.filter(network=current_network, flare_job_id=job_id).first()
                if not l_job:
                    logger.training.info(f"StatusAPI: Creating local mirror for job {job_id}")
                    l_job = TrainingJob.objects.create(
                        project=current_network.project,
                        network=current_network,
                        flare_job_id=job_id,
                        status="RUNNING"
                    )
                
                # Update rounds and progress
                if res["rounds_finished"] >= 0:
                    if l_job.rounds_finished is None or res["rounds_finished"] > l_job.rounds_finished:
                        logger.training.info(f"StatusAPI: Updating job {job_id} progress to round {res['rounds_finished']}")
                        l_job.rounds_finished = res["rounds_finished"]
                        total_rounds = l_job.total_rounds or 10
                        l_job.progress_percent = min(99, int((l_job.rounds_finished + 1) * 100 / total_rounds))
                        l_job.progress_updated_at = timezone.now()
                        l_job.save(update_fields=["rounds_finished", "progress_percent", "progress_updated_at"])
                
                # Update status if ended
                if res["ended"] and l_job.status == "RUNNING":
                    logger.training.info(f"StatusAPI: Job {job_id} marked as COMPLETED via docker logs")
                    l_job.status = "COMPLETED"
                    l_job.progress_percent = 100
                    l_job.completed_at = timezone.now()
                    l_job.save(update_fields=["status", "progress_percent", "completed_at"])
                
                # Use this job for the response
                if not job or l_job.created_at >= job.created_at:
                    job = l_job
    else:
        logger.training.debug("StatusAPI: No results from docker scraping")

    # 1. Mirror state from Server node if we are a client node (Keep as fallback)
    if not is_server_node and server_node and server_node.ip and server_node.ip != "-":
        try:
            state_url = f"https://{server_node.ip}:5085/training/api/state/{current_network.identifier}/"
            logger.training.debug(f"StatusAPI: Attempting to mirror state from server: {state_url}")
            resp = requests.get(state_url, timeout=2, verify=False)
            if resp.status_code == 200:
                remote_job = resp.json().get("job")
                if remote_job:
                    flare_id = remote_job["flare_job_id"]
                    logger.training.debug(f"StatusAPI: Received remote state for job {flare_id}")
                    j = TrainingJob.objects.filter(network=current_network, flare_job_id=flare_id).first()
                    if not j:
                        j = TrainingJob.objects.create(
                            project=current_network.project,
                            network=current_network,
                            flare_job_id=flare_id,
                            status=remote_job["status"]
                        )
                    
                    # Only update from remote if remote has more progress or terminal status
                    remote_rounds = remote_job.get("rounds_finished", 0)
                    if j.rounds_finished is None or remote_rounds > j.rounds_finished or remote_job["status"] in ["COMPLETED", "FAILED", "STOPPED"]:
                        j.status = remote_job["status"]
                        j.total_rounds = remote_job.get("total_rounds", j.total_rounds)
                        j.rounds_finished = remote_rounds
                        j.progress_percent = remote_job.get("progress_percent", j.progress_percent)
                        j.save()
                    
                    if not job or j.created_at >= job.created_at:
                        job = j
            else:
                logger.training.debug(f"StatusAPI: Server state API returned status {resp.status_code}")
        except Exception as e:
            logger.training.debug(f"StatusAPI: Failed to mirror state from server: {e}")

    nvflare_status = _nvflare_status_payload(current_network)
    if nvflare_status:
        logger.training.debug(f"StatusAPI: NVFlare admin API status: {nvflare_status}")
    
    if nvflare_status and nvflare_status.get("job_id"):
        status_map = {"RUNNING": "RUNNING", "COMPLETED": "COMPLETED", "STOPPED": "STOPPED", "FAILED": "FAILED"}
        status_key = str(nvflare_status.get("status", "")).upper().strip()
        mapped_status = status_map.get(status_key)
        
        if mapped_status:
            job_id_to_match = str(nvflare_status.get("job_id"))
            
            # Match existing job by exact ID or substring
            mirror_job = TrainingJob.objects.filter(
                network=current_network
            ).filter(
                models.Q(flare_job_id=job_id_to_match) | 
                models.Q(flare_job_id__icontains=job_id_to_match) |
                models.Q(flare_job_id__endswith=job_id_to_match)
            ).order_by("-created_at").first()
            
            if not mirror_job:
                mirror_job = TrainingJob.objects.create(
                    project=current_network.project, 
                    network=current_network, 
                    status=mapped_status, 
                    flare_job_id=job_id_to_match
                )
            elif mirror_job.status != mapped_status:
                mirror_job.status = mapped_status
                if mapped_status in {"COMPLETED", "STOPPED", "FAILED"}: mirror_job.completed_at = timezone.now()
                mirror_job.save(update_fields=["status", "completed_at"])
            
            if not job or (mirror_job and mirror_job.created_at >= job.created_at): 
                job = mirror_job

    if not job:
        if nvflare_status: 
            logger.training.debug("StatusAPI: Returning NVFlare admin status as fallback")
            return JsonResponse(nvflare_status)
        logger.training.debug("StatusAPI: No job found, returning idle")
        return JsonResponse({"status": "idle"})

    payload = _build_training_status_payload(current_network, job)
    logger.training.debug(f"StatusAPI: Final payload for {job.flare_job_id}: {payload}")
    
    # 2. Results Sync: Register results from local filesystem to local MinIO if COMPLETED
    if job.status == "COMPLETED":
        results_synced_key = f"results_synced_local_{job.identifier}"
        if not cache.get(results_synced_key):
            try:
                job_uuid = str(job.flare_job_id)
                match = re.search(r"([0-9a-f-]{36})", job_uuid)
                if match: job_uuid = match.group(1)
                
                # Path to local workspace where NVFlare produces results
                workspace_root = os.path.join(settings.BASE_DIR, "workspaces", str(job.project.identifier), str(current_network.identifier), "workspace")
                
                # List of possible result filenames produced by training
                possible_files = ["best_FL_model.pt", "model_weights.npz", "global_model.pt", "FL_model.pt"]
                
                found_and_synced = False
                from django.core.files.storage import default_storage
                from django.core.files.base import ContentFile

                for root, dirs, files in os.walk(workspace_root):
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
        should_override_status = (nv_status not in unknown_states and not (local_status in terminal_states and nv_status not in terminal_states))
        if should_override_status: payload["status"] = nvflare_status.get("status", payload["status"])
        payload["job_id"] = nvflare_status.get("job_id", payload["job_id"])
        nvflare_progress = nvflare_status.get("progress")
        if isinstance(nvflare_progress, (int, float)) and payload["progress"] < int(nvflare_progress):
            payload["progress"] = int(nvflare_progress)
    return JsonResponse(payload)


@login_required
@project_context_required
def training_logs_api(request):
    current_network = SwarmNetwork.resolve_current(request.user)
    if not current_network: return JsonResponse({"logs": []})
    logs = []
    try:
        job = TrainingJob.objects.filter(network=current_network).order_by("-created_at").first()
        if not job: return JsonResponse({"logs": logs})
        job_uuid = str(job.flare_job_id)
        match = re.search(r"Submitted job:\s*([0-9a-f-]+)", job_uuid)
        if match: job_uuid = match.group(1)
        else:
            match_uuid = re.search(r"([0-9a-f-]{36})", job_uuid)
            if match_uuid: job_uuid = match_uuid.group(1)
        cache_key = f"training_log_path_{job_uuid}"
        latest_log = cache.get(cache_key)
        if latest_log and not os.path.exists(latest_log):
            latest_log = None
            cache.delete(cache_key)
        if not latest_log:
            workspace_root = os.path.join("workspaces", str(job.project.identifier), str(current_network.identifier), "workspace")
            for root, _, files in os.walk(workspace_root):
                if job_uuid in root:
                    for cand in ("log_fl.txt", "log.txt"):
                        if cand in files:
                            latest_log = os.path.join(root, cand)
                            cache.set(cache_key, latest_log, 60)
                            break
                if latest_log: break
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
    except Exception as e:
        logger.training.debug(f"Error in training_logs_api: {e}")
    return JsonResponse({"logs": logs})
