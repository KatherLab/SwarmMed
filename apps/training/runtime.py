"""Shared training runtime helpers used by the UI, CLI, and tasks."""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import ssl
import threading
import time

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from common.utils import get_safe_slug
from logs.logger import get_logger

from .utils import (
    clamp_progress_percent,
    extract_flare_job_uuid,
    summarize_training_log,
    should_update_terminal_status,
)

logger = get_logger()


def build_training_status_payload(current_network, job):
    """Construct a canonical training status payload for UI and CLI consumers."""
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
        "created_at": job.created_at.isoformat(),
    }


def resolve_admin_startup_dir(current_network) -> str | None:
    """Resolve the NVFlare admin startup directory for a network."""
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
            candidate = os.path.join(prod_00_dir, item, "startup")
            if os.path.exists(candidate):
                return candidate

    return None


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


def resolve_admin_session_target(current_network) -> tuple[str, str, str] | None:
    """Resolve the NVFlare admin username, session directory, and server IP."""
    startup_dir = resolve_admin_startup_dir(current_network)
    if not startup_dir:
        return None

    startup_dir = os.path.abspath(startup_dir)
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
    for candidate in candidates:
        if not candidate:
            continue
        candidate = os.path.abspath(candidate)
        if os.path.exists(os.path.join(candidate, "startup", "fed_admin.json")):
            canonical = candidate
            break
        if os.path.exists(os.path.join(candidate, "fed_admin.json")):
            if os.path.basename(candidate.rstrip(os.sep)) == "startup":
                canonical = os.path.dirname(candidate.rstrip(os.sep))
            else:
                canonical = candidate
            break
        if os.path.exists(
            os.path.join(candidate, "startup", "startup", "fed_admin.json")
        ):
            canonical = os.path.join(candidate, "startup")
            break

    if canonical:
        session_dir = canonical

    for folder in ["local", "transfer", "logs"]:
        with contextlib.suppress(Exception):
            os.makedirs(os.path.join(session_dir, folder), exist_ok=True)

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
                    os.path.join(
                        "workspaces",
                        str(current_network.project.identifier),
                        str(current_network.identifier),
                        "workspace",
                        project_name,
                        "prod_00",
                        "startup",
                        "fed_client.json",
                    ),
                    os.path.join(
                        "workspaces",
                        str(current_network.project.identifier),
                        str(current_network.identifier),
                        "workspace",
                        "prod_00",
                        "startup",
                        "fed_client.json",
                    ),
                    os.path.join(session_dir, "startup", "fed_client.json"),
                ]
                for client_cfg in client_cfgs:
                    if not os.path.exists(client_cfg):
                        continue
                    with open(client_cfg) as handle:
                        data = json.load(handle)
                    client_name = data.get("client_name")
                    if client_name and client_name != "server":
                        admin_name = f"admin-{client_name}@nvidia.com"
                        break
            except Exception:
                pass

    server_ip = ""
    try:
        env_host = os.getenv("SWARMMEDHUB_SERVER_HOST", "").strip()
        if env_host:
            server_ip = env_host

        host_file = os.path.join(session_dir, "startup", "server_host.txt")
        if not server_ip and os.path.exists(host_file):
            with open(host_file) as handle:
                server_ip = handle.read().strip()
        else:
            prod_00 = os.path.dirname(session_dir.rstrip(os.sep))
            sibling_host = os.path.join(prod_00, "startup", "server_host.txt")
            if not server_ip and os.path.exists(sibling_host):
                with open(sibling_host) as handle:
                    server_ip = handle.read().strip()
            else:
                client_cfg = os.path.join(prod_00, "startup", "fed_client.json")
                if not server_ip and os.path.exists(client_cfg):
                    from network.tasks import _extract_host_from_server_endpoint

                    extracted = _extract_host_from_server_endpoint(
                        os.path.dirname(client_cfg)
                    )
                    if extracted:
                        server_ip = extracted
    except Exception:
        pass

    return admin_name, session_dir, server_ip


def parse_nvflare_jobs(response):
    """Parse NVFlare job records from API or CLI output."""
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
    status_re = re.compile(r"\b(RUNNING|COMPLETED|FAILED|STOPPED)\b", re.I)
    for line in text.splitlines():
        uuid_match = re.search(r"([0-9a-f-]{36})", line)
        status_match = status_re.search(line)
        if uuid_match:
            jobs.append(
                {
                    "job_id": uuid_match.group(1),
                    "status": (
                        status_match.group(1).upper() if status_match else "UNKNOWN"
                    ),
                }
            )
    return jobs


def select_nvflare_job(jobs):
    """Prefer the running NVFlare job, else return the first visible job."""
    if not jobs:
        return None

    def status_of(job):
        return str(
            job.get("status") or job.get("job_status") or job.get("state") or ""
        ).upper().strip()

    running = [job for job in jobs if status_of(job) == "RUNNING"]
    if running:
        return running[0]
    return jobs[0]


def dedupe_keep_order(values):
    """Remove duplicates while preserving the original order."""
    deduped = []
    for value in values:
        if value in deduped:
            continue
        deduped.append(value)
    return deduped


_NVFLARE_GRPC_PATCHED = False
_NVFLARE_GRPC_PATCH_LOCK = threading.Lock()


def _ensure_grpc_ssl_patched(tls_server_name: str) -> None:
    global _NVFLARE_GRPC_PATCHED
    if _NVFLARE_GRPC_PATCHED:
        return
    with _NVFLARE_GRPC_PATCH_LOCK:
        if _NVFLARE_GRPC_PATCHED:
            return
        try:
            import grpc as grpc_module

            original_secure_channel = grpc_module.secure_channel

            def _patched(target, credentials, options=None, **kwargs):
                opts = list(options or [])
                if not any(
                    isinstance(option, (list, tuple))
                    and len(option) >= 1
                    and option[0] == "grpc.ssl_target_name_override"
                    for option in opts
                ):
                    opts.append(
                        ("grpc.ssl_target_name_override", tls_server_name)
                    )
                return original_secure_channel(
                    target, credentials, options=opts, **kwargs
                )

            grpc_module.secure_channel = _patched
            _NVFLARE_GRPC_PATCHED = True
        except Exception:
            return


def _build_flare_host_candidates(requested_host: str, default_host: str = ""):
    requested_host = (requested_host or "").strip()
    default_host = (default_host or "").strip()
    seed_candidates = [
        "server",
        requested_host,
        default_host,
        "127.0.0.1",
        "localhost",
        "",
    ]
    return dedupe_keep_order([candidate for candidate in seed_candidates if candidate])


def _build_flare_port_candidates(default_port: int = 0):
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


def log_flare_pre_submit_diagnostics(
    log, username: str, startup_kit_location: str, requested_host: str
):
    """Log NVFlare connectivity diagnostics before job submission."""
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
                    with socket.create_connection(
                        (effective_host, int(port)), timeout=2.0
                    ):
                        reachable = True
                except Exception as exc:
                    detail = str(exc)
                log.training.info(
                    "FLARE connectivity probe: "
                    f"host={effective_host} port={int(port)} reachable={reachable}"
                    + (f" detail={detail}" if detail else "")
                )
                if not (
                    reachable
                    and os.path.exists(ca_cert_path)
                    and os.path.exists(client_cert_path)
                    and os.path.exists(client_key_path)
                ):
                    continue

                tls_ok = False
                tls_detail = ""
                peer_cn = ""
                peer_san = ""
                try:
                    tls_ctx = ssl.create_default_context(cafile=ca_cert_path)
                    tls_ctx.check_hostname = False
                    tls_ctx.load_cert_chain(
                        certfile=client_cert_path, keyfile=client_key_path
                    )
                    with socket.create_connection(
                        (effective_host, int(port)), timeout=2.5
                    ) as raw_sock:
                        with tls_ctx.wrap_socket(
                            raw_sock, server_hostname=effective_host
                        ) as tls_sock:
                            cert = tls_sock.getpeercert()
                            subject = cert.get("subject", []) if isinstance(cert, dict) else []
                            for subject_item in subject:
                                for key, value in subject_item:
                                    if key == "commonName":
                                        peer_cn = str(value)
                                        break
                                if peer_cn:
                                    break
                            san_entries = (
                                cert.get("subjectAltName", [])
                                if isinstance(cert, dict)
                                else []
                            )
                            if san_entries:
                                peer_san = ",".join(
                                    f"{san_type}:{san_value}"
                                    for san_type, san_value in san_entries
                                )
                            tls_ok = True
                except Exception as exc:
                    tls_detail = str(exc)
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
            if probe_session.api and not getattr(probe_session.api, "cell", None):
                probe_session.api.closed = True
            else:
                probe_session.close()
        except Exception:
            pass
    except Exception as exc:
        log.training.warning(f"FLARE pre-submit diagnostics failed: {exc}")


def parse_nvflare_clients(response) -> list[str]:
    """Extract a client-name list from NVFlare CLI/API output."""
    clients = []
    if isinstance(response, dict):
        data = response.get("data") or response.get("clients") or []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("client_name")
                    if name:
                        clients.append(str(name))
                elif isinstance(item, str):
                    clients.append(item)
    if not clients:
        text = str(response or "")
        for line in text.splitlines():
            parts = [part.strip() for part in line.split("|") if part.strip()]
            if len(parts) >= 2:
                name = parts[0]
                if name.lower() not in {
                    "client name",
                    "name",
                    "-------",
                    "server",
                    "admin",
                }:
                    clients.append(name)
    return dedupe_keep_order(clients)


def new_secure_session_with_host(
    username: str,
    startup_kit_location: str,
    host: str,
    debug: bool = False,
    timeout: float = 20.0,
    network_id=None,
):
    """Create a secure NVFlare admin session with host fallback probing."""
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
            with contextlib.suppress(Exception):
                temp_session.close()
    except Exception:
        pass

    canonical_host = canonical_host or "server"
    admin_port = admin_port if admin_port > 0 else 8003

    requested_host = (host or "").strip()
    ip_candidates = [canonical_host, requested_host]
    if network_id:
        ip_candidates.append(f"swarm-{str(network_id)[:12]}-server")

    docker_host_ip = os.getenv("DOCKER_HOST_IP", "172.17.0.1")
    ip_candidates.extend(
        [docker_host_ip, "host.docker.internal", "127.0.0.1", "localhost"]
    )
    ip_candidates = dedupe_keep_order([candidate for candidate in ip_candidates if candidate])

    _ensure_grpc_ssl_patched(canonical_host)

    reachable: set[str] = set()
    for ip in ip_candidates:
        try:
            with socket.create_connection((ip, admin_port), timeout=1.0):
                reachable.add(ip)
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
                with contextlib.suppress(Exception):
                    session.api.authenticate_msg_timeout = max(
                        float(timeout),
                        float(
                            getattr(session.api, "authenticate_msg_timeout", 5.0)
                            or 5.0
                        ),
                    )

                session.api.host = candidate
                session.api.port = int(admin_port)

                try:
                    session.try_connect(connect_timeout)
                    deadline = time.monotonic() + min(connect_timeout, 8.0)
                    while (
                        getattr(session.api, "cell", None) is None
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.15)

                    if getattr(session.api, "cell", None) is not None:
                        time.sleep(2.0)

                    try:
                        submit_cmd_info = session.api.check_command("submit_job")
                    except Exception as exc:
                        connection_errors.append(
                            f"host={candidate} port={admin_port}: "
                            f"connected but submit command probe failed: {exc}"
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
                except Exception as exc:
                    connection_errors.append(f"host={candidate} port={admin_port}: {exc}")
            else:
                session.try_connect(connect_timeout)
                deadline = time.monotonic() + min(connect_timeout, 8.0)
                while (
                    getattr(session.api, "cell", None) is None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.15)
                if getattr(session.api, "cell", None) is not None:
                    time.sleep(2.0)
                return session
        except Exception as exc:
            connection_errors.append(f"host={candidate} port={admin_port}: {exc}")

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


def nvflare_status_payload(current_network):
    """Fetch the current NVFlare job status directly from the admin API."""
    cache_key = f"nvflare_status_payload_{current_network.identifier}"
    cached_payload = cache.get(cache_key)
    if cached_payload is not None:
        return cached_payload

    admin_target = resolve_admin_session_target(current_network)
    if not admin_target:
        cache.set(cache_key, None, 15)
        return None

    try:
        admin_name, admin_dir, server_ip = admin_target
        session = new_secure_session_with_host(
            username=admin_name,
            startup_kit_location=admin_dir,
            host=server_ip,
            timeout=15.0,
            network_id=current_network.identifier,
        )
        response = session.api.do_command("list_jobs")
        with contextlib.suppress(Exception):
            session.close()

        jobs = parse_nvflare_jobs(response)
        job = select_nvflare_job(jobs)
        if not job:
            cache.set(cache_key, None, 15)
            return None

        status = str(
            job.get("status") or job.get("job_status") or job.get("state") or "UNKNOWN"
        ).upper().strip()
        job_id = job.get("job_id") or job.get("id") or "unknown"
        if status in {"", "UNKNOWN", "N/A", "NONE"}:
            cache.set(cache_key, None, 15)
            return None

        payload = {
            "status": status.title(),
            "progress": 100 if status == "COMPLETED" else 0,
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


def tail_text(file_path: str, max_bytes: int = 2048 * 1024) -> str:
    """Read the tail end of a text file."""
    try:
        with open(file_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            start = max(0, end - max_bytes)
            handle.seek(start)
            data = handle.read()
        return data.decode("utf-8", errors="ignore")
    except OSError:
        return ""


def find_latest_training_log(
    project_id: str,
    network_id: str,
    job_uuid: str,
    cache_ttl_seconds: int = 60,
) -> str | None:
    """Locate the most recent runtime training log for a job."""
    cache_key = f"training_log_path_{project_id}_{network_id}_{job_uuid}"
    cached_path = cache.get(cache_key)
    if cached_path and os.path.exists(cached_path):
        return cached_path

    workspace_root = os.path.join("workspaces", project_id, network_id, "workspace")
    if not os.path.exists(workspace_root):
        return None

    latest_log = None
    for root, dirs, files in os.walk(workspace_root):
        dirs[:] = [directory for directory in dirs if not directory.startswith(".") and directory != "__pycache__"]
        if job_uuid in root:
            for candidate in ("log_fl.txt", "log.txt"):
                if candidate in files:
                    latest_log = os.path.join(root, candidate)
                    break
        if latest_log:
            break

    if latest_log and os.path.exists(latest_log):
        cache.set(cache_key, latest_log, cache_ttl_seconds)
        return latest_log
    return None


def format_duration(seconds):
    """Format seconds into a short human-readable duration."""
    if seconds < 0:
        return "0s"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def get_training_progress_info(training_job, current_network):
    """Calculate training progress from persisted state and workspace logs."""
    cache_key = f"training_progress_{training_job.id}_{training_job.status}"
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        return cached_result

    training_progress = 0
    training_status = training_job.status.title()
    is_training_running = False
    duration_str = "-"
    eta_str = "-"

    job_uuid = (
        training_job.flare_job_uuid
        or extract_flare_job_uuid(training_job.flare_job_id)
        or str(training_job.flare_job_id or "")
    )

    if training_job.status == "RUNNING":
        is_training_running = True
        have_cached_progress = False
        try:
            if training_job.progress_updated_at:
                age = (
                    timezone.now() - training_job.progress_updated_at
                ).total_seconds()
                if age <= 60 and training_job.progress_percent is not None:
                    training_progress = clamp_progress_percent(
                        training_job.progress_percent, status="RUNNING"
                    ) or 0
                    have_cached_progress = True
        except (TypeError, ValueError):
            have_cached_progress = False

        try:
            total_rounds = training_job.total_rounds or 10
            workspace_dir = os.path.join(
                "workspaces",
                str(training_job.project.identifier),
                str(current_network.identifier),
                "workspace",
            )
            for root, _dirs, files in os.walk(workspace_dir):
                if "config_fed_server.json" not in files:
                    continue
                try:
                    with open(os.path.join(root, "config_fed_server.json")) as handle:
                        cfg = json.load(handle)
                    for workflow in cfg.get("workflows", []):
                        if workflow.get("id") == "swarm_controller":
                            total_rounds = int(
                                workflow.get("args", {}).get("num_rounds", total_rounds)
                            )
                            break
                except Exception:
                    pass
                if total_rounds != 10:
                    break

            rounds_finished = training_job.rounds_finished or 0
            terminal_status = None
            if have_cached_progress:
                rounds_finished = training_job.rounds_finished or 0

            def scan_log_tail(file_path: str) -> None:
                nonlocal terminal_status, rounds_finished
                data = tail_text(file_path, max_bytes=512 * 1024)
                if not data:
                    return
                summary = summarize_training_log(data)
                if summary["rounds_finished"] > rounds_finished:
                    rounds_finished = summary["rounds_finished"]
                if should_update_terminal_status(
                    terminal_status, summary["terminal_status"]
                ):
                    terminal_status = summary["terminal_status"]

            if not have_cached_progress:
                for root, _dirs, files in os.walk(workspace_dir):
                    if job_uuid not in root:
                        continue
                    for file_name in files:
                        if file_name.startswith("log") and file_name.endswith(".txt"):
                            scan_log_tail(os.path.join(root, file_name))
                            if terminal_status:
                                break
                    if terminal_status:
                        break

            if terminal_status:
                if not have_cached_progress and total_rounds > 0:
                    rounds_completed = rounds_finished + 1 if rounds_finished >= 0 else 0
                    training_progress = min(
                        99, int(rounds_completed * 100 / total_rounds)
                    )

                if terminal_status == "COMPLETED":
                    training_progress = 100

                training_status = terminal_status.title()
                is_training_running = False
            elif not have_cached_progress and total_rounds > 0:
                rounds_completed = rounds_finished + 1 if rounds_finished >= 0 else 0
                training_progress = min(99, int(rounds_completed * 100 / total_rounds))
        except Exception as exc:
            logger.training.debug(f"Failed to calculate training progress: {exc}")
            training_progress = 0

    elif training_job.status == "COMPLETED":
        training_progress = 100
        training_status = "Completed"
    elif training_job.status in {"FAILED", "STOPPED"}:
        training_progress = clamp_progress_percent(
            training_job.progress_percent or 0, status=training_job.status
        ) or 0
        training_status = training_job.status.title()

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

    result = {
        "status": training_status,
        "progress": training_progress,
        "duration": duration_str,
        "eta": eta_str,
        "is_running": is_training_running,
    }
    if training_job.status == "RUNNING":
        cache.set(cache_key, result, 5)
    return result
