"""
Celery tasks for the network app.
Handles long-running operations like Docker deployment,
preflight checks, and real-time log streaming.
"""

import os
import json
import re
import socket
import stat
import shutil
import ipaddress
import subprocess  # nosec B404
import time
from pathlib import Path
from urllib.parse import urlparse

import docker
import yaml
from celery import shared_task
from common.utils import get_s3_client
from django.conf import settings
from django.contrib.auth.models import User
from django.utils.text import slugify
from logs.logger import get_logger
from logs.models import LogCategory, LogEntry
from project.models import Project

from .models import SwarmNetwork
from .utils import get_hostname, get_tailscale_ip


def run_and_log_subprocess(command, cwd, env, logger):
    """
    Executes a subprocess and streams its output to the provided logger.
    """
    process = subprocess.Popen(  # nosec B603
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    for line in process.stdout:
        line = line.strip()
        if line:
            # Use the network category for these logs
            logger.network.info(line)

    process.wait()
    return process.returncode


def _docker_network_name(network_identifier):
    return f"swarm_{str(network_identifier)[:12]}_net"


def _container_name_for(network_identifier, participant_name):
    safe_participant = "".join(
        c if c.isalnum() or c in {"-", "_"} else "-"
        for c in str(participant_name)
    ).strip("-")
    return f"swarm-{str(network_identifier)[:12]}-{safe_participant}"[:63]


def _load_json_file(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _ensure_executable(path):
    if not os.path.exists(path):
        return
    current_mode = os.stat(path).st_mode
    os.chmod(path, current_mode | stat.S_IXUSR)


def _extract_host_from_server_endpoint(startup_dir):
    fed_client_json = os.path.join(startup_dir, "fed_client.json")
    config = _load_json_file(fed_client_json)

    agent_args = config.get("overseer_agent", {}).get("args", {})
    endpoint = agent_args.get("sp_end_point") or agent_args.get(
        "overseer_end_point", ""
    )
    if not endpoint:
        return ""

    if "://" in endpoint:
        parsed = urlparse(endpoint)
        host = (parsed.hostname or "").strip()
    else:
        host = endpoint.split(":")[0].strip()

    if host in {"", "server", "localhost", "127.0.0.1"}:
        return ""
    return host


def _endpoint_with_localhost(endpoint: str) -> str:
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return ""

    if "://" not in endpoint:
        return ""

    try:
        parsed = urlparse(endpoint)
        if not parsed.scheme or not parsed.netloc:
            return ""

        host = parsed.hostname
        if not host:
            return ""

        netloc = "127.0.0.1"
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"

        return parsed._replace(netloc=netloc).geturl()
    except Exception:
        return ""


def _discover_runtime_targets(base_prod_path):
    targets = []
    if not os.path.isdir(base_prod_path):
        return targets

    root_startup = os.path.join(base_prod_path, "startup")
    if os.path.isdir(root_startup):
        fed_server_root = os.path.join(root_startup, "fed_server.json")
        fed_client_root = os.path.join(root_startup, "fed_client.json")
        if os.path.exists(fed_server_root):
            targets.append(
                {
                    "name": "server",
                    "role": "server",
                    "path": base_prod_path,
                    "startup_dir": root_startup,
                }
            )
        elif os.path.exists(fed_client_root):
            fed_client_cfg = _load_json_file(fed_client_root)
            client_name = (
                fed_client_cfg.get("overseer_agent", {})
                .get("args", {})
                .get("name", "client")
            )
            targets.append(
                {
                    "name": str(client_name),
                    "role": "client",
                    "path": base_prod_path,
                    "startup_dir": root_startup,
                }
            )

    for participant in sorted(os.scandir(base_prod_path), key=lambda p: p.name):
        if not participant.is_dir():
            continue

        startup_dir = os.path.join(participant.path, "startup")
        if not os.path.isdir(startup_dir):
            continue

        if participant.name.startswith("admin"):
            continue

        fed_server_path = os.path.join(startup_dir, "fed_server.json")
        fed_client_path = os.path.join(startup_dir, "fed_client.json")

        if os.path.exists(fed_server_path):
            role = "server"
        elif os.path.exists(fed_client_path):
            role = "client"
        else:
            continue

        targets.append(
            {
                "name": participant.name,
                "role": role,
                "path": participant.path,
                "startup_dir": startup_dir,
            }
        )

    role_order = {"server": 0, "client": 1}
    targets.sort(key=lambda t: (role_order.get(t["role"], 9), t["name"]))
    return targets


def _read_local_hostname_candidates():
    names = set()

    env_hostname = os.getenv("SWARMCLOUD_HOSTNAME", "").strip()
    if env_hostname:
        names.add(env_hostname)

    try:
        runtime_hostname = str(get_hostname() or "").strip()
        if runtime_hostname:
            names.add(runtime_hostname)
    except Exception:
        pass

    explicit_participant = os.getenv("SWARMCLOUD_LOCAL_PARTICIPANT", "").strip()
    if explicit_participant:
        names.add(explicit_participant)

    hostname_file = Path(settings.BASE_DIR) / ".swarmcloud_hostname"
    if hostname_file.exists():
        try:
            file_hostname = hostname_file.read_text().strip()
            if file_hostname:
                names.add(file_hostname)
        except Exception:
            pass

    normalized = set()
    for name in names:
        normalized.add(name)
        safe_name = slugify(name)
        if safe_name:
            normalized.add(safe_name)

    return normalized


def _read_local_client_names_metadata(base_prod_path):
    metadata_path = Path(base_prod_path) / ".local_client_names.json"
    if not metadata_path.exists():
        return set()

    try:
        payload = json.loads(metadata_path.read_text())
    except Exception:
        return set()

    if not isinstance(payload, list):
        return set()

    names = set()
    for entry in payload:
        if isinstance(entry, str) and entry.strip():
            names.add(entry.strip())
    return names


def _read_project_client_ip_map(project_yml_path):
    if not os.path.exists(project_yml_path):
        return {}

    try:
        with open(project_yml_path) as f:
            project_yml = yaml.safe_load(f) or {}
    except Exception:
        return {}

    if not isinstance(project_yml, dict):
        return {}

    client_map = {}
    for participant in project_yml.get("participants", []):
        if not isinstance(participant, dict):
            continue
        if participant.get("type") != "client":
            continue

        participant_name = str(participant.get("name", "")).strip()
        participant_ip = str(participant.get("listening_host", "")).strip()
        if participant_name and participant_ip:
            client_map[participant_name] = participant_ip

    return client_map


def _resolve_local_client_names(project_yml_path, base_prod_path):
    candidates = _read_local_client_names_metadata(base_prod_path)
    candidates.update(_read_local_hostname_candidates())

    local_ip = str(get_tailscale_ip() or "").strip()
    try:
        ipaddress.ip_address(local_ip)
        valid_local_ip = True
    except ValueError:
        valid_local_ip = False

    if valid_local_ip:
        project_client_ip_map = _read_project_client_ip_map(project_yml_path)
        for participant_name, participant_ip in project_client_ip_map.items():
            if participant_ip == local_ip:
                candidates.add(participant_name)

    return candidates


def _list_existing_docker_subnets(docker_path, env):
    ls_result = subprocess.run(  # nosec B603
        [docker_path, "network", "ls", "-q"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if ls_result.returncode != 0:
        return set()

    network_ids = [n.strip() for n in ls_result.stdout.splitlines() if n.strip()]
    if not network_ids:
        return set()

    inspect_result = subprocess.run(  # nosec B603
        [docker_path, "network", "inspect", *network_ids],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if inspect_result.returncode != 0:
        return set()

    subnets = set()
    try:
        inspected = json.loads(inspect_result.stdout or "[]")
        for network in inspected:
            for ipam_config in (network.get("IPAM", {}).get("Config") or []):
                subnet = (ipam_config or {}).get("Subnet")
                if not subnet:
                    continue
                try:
                    subnets.add(ipaddress.ip_network(subnet, strict=False))
                except ValueError:
                    continue
    except Exception:
        return set()

    return subnets


def _candidate_subnets_for_network(network_identifier, existing_subnets):
    hash_seed = abs(hash(str(network_identifier)))
    candidates = []

    for second_octet in (240, 241, 242, 243):
        for third_octet in range(0, 256):
            candidates.append(ipaddress.ip_network(f"10.{second_octet}.{third_octet}.0/24"))

    start_index = hash_seed % len(candidates)
    ordered_candidates = candidates[start_index:] + candidates[:start_index]

    available = []
    for candidate in ordered_candidates:
        if any(candidate.overlaps(existing) for existing in existing_subnets):
            continue
        available.append(candidate)
        if len(available) >= 24:
            break
    return available


def _ensure_docker_network(docker_path, network_name, env, logger):
    inspect = subprocess.run(  # nosec B603
        [docker_path, "network", "inspect", network_name],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if inspect.returncode == 0:
        return

    create = subprocess.run(  # nosec B603
        [docker_path, "network", "create", network_name],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if create.returncode != 0:
        error_text = (create.stderr or "").strip()
        lower_error = error_text.lower()
        allocation_issue = any(
            marker in lower_error
            for marker in [
                "non-overlapping ipv4 address pool",
                "predefined address pools have been fully subnetted",
                "address pools have been fully subnetted",
            ]
        )

        if not allocation_issue:
            raise RuntimeError(
                f"Failed to create Docker network '{network_name}': {error_text}"
            )

        logger.network.warning(
            "Docker default address pools are exhausted/overlapping; retrying with explicit subnet selection."
        )
        existing_subnets = _list_existing_docker_subnets(docker_path, env)
        fallback_subnets = _candidate_subnets_for_network(
            network_identifier=network_name,
            existing_subnets=existing_subnets,
        )

        last_error = error_text
        for subnet in fallback_subnets:
            retry = subprocess.run(  # nosec B603
                [
                    docker_path,
                    "network",
                    "create",
                    "--subnet",
                    str(subnet),
                    network_name,
                ],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            if retry.returncode == 0:
                logger.network.info(
                    f"Created Docker network '{network_name}' with fallback subnet {subnet}."
                )
                return
            last_error = (retry.stderr or "").strip() or last_error

        raise RuntimeError(
            f"Failed to create Docker network '{network_name}' after subnet fallback attempts: {last_error}"
        )


def _stop_labeled_runtime(network_id, docker_path, env, logger):
    label = f"swarmcloud.network_id={network_id}"
    result = subprocess.run(  # nosec B603
        [
            docker_path,
            "ps",
            "-a",
            "--filter",
            f"label={label}",
            "--format",
            "{{.ID}} {{.Names}}",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    removed = 0
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if not parts:
                continue
            container_id = parts[0]
            container_name = parts[1] if len(parts) > 1 else container_id

            logger.network.info(f"Stopping container {container_name}...")
            subprocess.run(  # nosec B603
                [docker_path, "stop", "-t", "15", container_id],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            subprocess.run(  # nosec B603
                [docker_path, "rm", "-f", container_id],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            removed += 1

    network_name = _docker_network_name(network_id)
    subprocess.run(  # nosec B603
        [docker_path, "network", "rm", network_name],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    return removed


def _build_local_fallback_image(
    docker_path,
    env,
    logger,
    image_name,
    provision_dir,
    base_prod_path,
):
    build_dir = os.path.join(base_prod_path, ".swarmcloud_runtime_build")
    os.makedirs(build_dir, exist_ok=True)

    requirements_src = _resolve_runtime_requirements_path(
        provision_dir=provision_dir,
        base_prod_path=base_prod_path,
    )
    requirements_dst = os.path.join(build_dir, "requirements.txt")

    if requirements_src and os.path.exists(requirements_src):
        shutil.copyfile(requirements_src, requirements_dst)
    else:
        with open(requirements_dst, "w") as rf:
            rf.write("nvflare==2.7.1\n")
            rf.write("gunicorn\n")
            rf.write("boto3\n")
            rf.write("python-dotenv\n")
            rf.write("pandas\n")
            rf.write("numpy\n")

    dockerfile_path = os.path.join(build_dir, "Dockerfile")
    with open(dockerfile_path, "w") as df:
        df.write("FROM python:3.12-slim\n")
        df.write("ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\n")
        df.write(
            "RUN apt-get update && apt-get install -y --no-install-recommends bash && rm -rf /var/lib/apt/lists/*\n"
        )
        df.write("RUN pip install --no-cache-dir --upgrade pip\n")
        df.write("COPY requirements.txt /tmp/requirements.txt\n")
        df.write("RUN pip install --no-cache-dir -r /tmp/requirements.txt\n")

    logger.network.info(
        f"Building fallback runtime image for network: {image_name}"
    )
    _build_image_with_compat(
        docker_path=docker_path,
        image_name=image_name,
        cwd=build_dir,
        env=env,
        logger=logger,
        error_command="docker build fallback",
    )


def _resolve_runtime_requirements_path(provision_dir, base_prod_path):
    candidates = [
        os.path.join(provision_dir, "docker_compose_requirements.txt"),
        os.path.join(base_prod_path, "docker_compose_requirements.txt"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return ""


def _parse_safe_requirement_lines(requirements_text):
    safe_lines = []
    seen = set()
    for raw_line in (requirements_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(
            r"^[a-zA-Z0-9_\-\[\]]+([=<>!~]+[a-zA-Z0-9\._\-\*\,]+)?$",
            line,
        ):
            normalized = line.lower()
            if normalized not in seen:
                safe_lines.append(line)
                seen.add(normalized)
    return safe_lines


def _collect_project_runtime_requirements(project, logger):
    baseline = [
        "nvflare==2.7.1",
        "gunicorn",
        "boto3",
        "python-dotenv",
        "pandas",
        "numpy",
    ]

    merged = []
    seen = set()

    def _add_lines(lines):
        for line in lines:
            key = line.strip().lower()
            if key and key not in seen:
                merged.append(line)
                seen.add(key)

    _add_lines(baseline)

    requirement_sources = []
    if getattr(project, "requirements_file", None):
        try:
            if project.requirements_file.name:
                requirement_sources.append(project.requirements_file.name)
        except Exception:
            pass

    requirement_sources.append(
        f"{project.identifier}/code/training/requirements.txt"
    )
    requirement_sources.append(
        f"{project.identifier}/code/requirements/requirements.txt"
    )

    bucket = settings.AWS_STORAGE_BUCKET_NAME
    s3_client = get_s3_client()

    try:
        prefixes = [
            f"{project.identifier}/code/requirements/",
            f"{project.identifier}/code/training/",
        ]
        for prefix in prefixes:
            continuation_token = None
            while True:
                kwargs = {
                    "Bucket": bucket,
                    "Prefix": prefix,
                    "MaxKeys": 100,
                }
                if continuation_token:
                    kwargs["ContinuationToken"] = continuation_token

                response = s3_client.list_objects_v2(**kwargs)
                for obj in response.get("Contents", []):
                    key = str(obj.get("Key", "")).strip()
                    lower_key = key.lower()
                    if not key:
                        continue
                    if lower_key.endswith(".txt") and "requirements" in lower_key:
                        requirement_sources.append(key)

                if not response.get("IsTruncated"):
                    break
                continuation_token = response.get("NextContinuationToken")
    except Exception as e:
        logger.network.info(
            f"Could not enumerate requirement files in project storage: {e}"
        )

    # Preserve order while removing duplicates
    seen_keys = set()
    deduped_sources = []
    for key in requirement_sources:
        if key and key not in seen_keys:
            deduped_sources.append(key)
            seen_keys.add(key)

    for key in deduped_sources:
        try:
            response = s3_client.get_object(Bucket=bucket, Key=key)
            text = response["Body"].read().decode("utf-8")
            lines = _parse_safe_requirement_lines(text)
            if lines:
                logger.network.info(
                    f"Loaded runtime requirements from storage key: {key}"
                )
                _add_lines(lines)
        except Exception as e:
            logger.network.info(
                f"No readable requirements found at {key}: {e}"
            )

    return merged


def _ensure_runtime_requirements_file(
    swarm_network,
    provision_dir,
    base_prod_path,
    logger,
):
    existing_path = _resolve_runtime_requirements_path(
        provision_dir=provision_dir,
        base_prod_path=base_prod_path,
    )

    merged = []
    seen = set()

    def _add_lines(lines):
        for line in lines:
            key = line.strip().lower()
            if key and key not in seen:
                merged.append(line)
                seen.add(key)

    if existing_path:
        try:
            with open(existing_path) as rf:
                _add_lines(_parse_safe_requirement_lines(rf.read()))
            logger.network.info(
                f"Using existing runtime requirements file: {existing_path}"
            )
        except Exception as e:
            logger.network.warning(
                f"Failed reading existing runtime requirements file {existing_path}: {e}"
            )

    try:
        s3_lines = _collect_project_runtime_requirements(
            project=swarm_network.project,
            logger=logger,
        )
        _add_lines(s3_lines)
    except Exception as e:
        logger.network.warning(
            f"Failed collecting runtime requirements from project storage: {e}"
        )

    if not merged:
        return ""

    os.makedirs(provision_dir, exist_ok=True)
    out_path = os.path.join(provision_dir, "docker_compose_requirements.txt")
    try:
        with open(out_path, "w") as wf:
            wf.write("\n".join(merged) + "\n")
        logger.network.info(
            f"Materialized runtime requirements file at: {out_path}"
        )
        return out_path
    except Exception as e:
        logger.network.warning(
            f"Failed writing runtime requirements file {out_path}: {e}"
        )
        return existing_path


def _has_custom_runtime_requirements(provision_dir, base_prod_path):
    req_path = _resolve_runtime_requirements_path(
        provision_dir=provision_dir,
        base_prod_path=base_prod_path,
    )
    if not req_path:
        return False

    baseline_prefixes = {
        "nvflare",
        "gunicorn",
        "boto3",
        "python-dotenv",
        "pandas",
        "numpy",
    }

    try:
        with open(req_path) as rf:
            for raw_line in rf:
                line = raw_line.strip().lower()
                if not line or line.startswith("#"):
                    continue

                pkg_name = re.split(r"[=<>!~\[]", line, maxsplit=1)[0].strip()
                if pkg_name and pkg_name not in baseline_prefixes:
                    return True
    except Exception:
        return False

    return False


def _build_image_with_compat(
    docker_path,
    image_name,
    cwd,
    env,
    logger,
    error_command,
):
    build_cmd = [docker_path, "build", "-t", image_name, "."]
    require_buildkit = (
        os.getenv("SWARMCLOUD_REQUIRE_BUILDKIT", "")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )
    allow_legacy_builder = (
        os.getenv("SWARMCLOUD_ALLOW_LEGACY_DOCKER_BUILDER", "")
        .strip()
        .lower()
        in {"1", "true", "yes", "on"}
    )

    buildx_check = subprocess.run(  # nosec B603
        [docker_path, "buildx", "version"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    buildx_available = buildx_check.returncode == 0

    if buildx_available:
        primary_env = env.copy()
        primary_env["DOCKER_BUILDKIT"] = "1"

        ret = run_and_log_subprocess(
            build_cmd,
            cwd=cwd,
            env=primary_env,
            logger=logger,
        )
        if ret == 0:
            return

        if require_buildkit and not allow_legacy_builder:
            logger.network.error(
                "Docker image build failed with BuildKit enabled and strict BuildKit mode is active. "
                "Set SWARMCLOUD_ALLOW_LEGACY_DOCKER_BUILDER=true to permit DOCKER_BUILDKIT=0 fallback."
            )
            raise subprocess.CalledProcessError(ret, error_command)

        if not allow_legacy_builder:
            logger.network.warning(
                "Docker image build failed with BuildKit; falling back to deprecated DOCKER_BUILDKIT=0 mode."
            )
    else:
        logger.network.warning(
            "BuildKit/buildx is unavailable; using deprecated DOCKER_BUILDKIT=0 compatibility mode."
        )
        if require_buildkit and not allow_legacy_builder:
            logger.network.error(
                "BuildKit/buildx is required by SWARMCLOUD_REQUIRE_BUILDKIT, but buildx is unavailable."
            )
            raise subprocess.CalledProcessError(1, error_command)

    if require_buildkit and not allow_legacy_builder and not buildx_available:
        logger.network.error(
            "Legacy builder fallback is disabled by strict BuildKit settings."
        )
        raise subprocess.CalledProcessError(1, error_command)

    compat_env = env.copy()
    compat_env["DOCKER_BUILDKIT"] = "0"
    ret = run_and_log_subprocess(
        build_cmd,
        cwd=cwd,
        env=compat_env,
        logger=logger,
    )
    if ret != 0:
        raise subprocess.CalledProcessError(ret, error_command)


def _is_host_port_available(port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False


def _add_port_mapping_with_fallback(
    run_cmd,
    container_port,
    logger,
    participant_name,
):
    container_port = int(container_port)
    if _is_host_port_available(container_port):
        run_cmd.extend(["-p", f"{container_port}:{container_port}"])
    else:
        logger.network.warning(
            f"Port {container_port} is already in use; using dynamic host port for {participant_name}."
        )
        # Keep container port fixed, let Docker choose an available host port.
        run_cmd.extend(["-p", str(container_port)])


def _add_required_port_mapping(
    run_cmd,
    container_port,
    logger,
    participant_name,
):
    container_port = int(container_port)
    if not _is_host_port_available(container_port):
        raise RuntimeError(
            f"Required host port {container_port} is already in use for {participant_name}. "
            "Server/admin ports must be fixed for remote FLARE connectivity."
        )

    logger.network.info(
        f"Using fixed host port mapping {container_port}:{container_port} for {participant_name}."
    )
    run_cmd.extend(["-p", f"{container_port}:{container_port}"])


def _get_container_last_logs(docker_path, container_name, env, lines=40):
    result = subprocess.run(  # nosec B603
        [docker_path, "logs", "--tail", str(lines), container_name],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    return output.strip()


def _is_container_running(docker_path, container_name, env):
    result = subprocess.run(  # nosec B603
        [docker_path, "inspect", "-f", "{{.State.Running}}", container_name],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        return False
    return result.stdout.strip().lower() == "true"


def _count_running_labeled_containers(network_id, docker_path, env):
    result = subprocess.run(  # nosec B603
        [
            docker_path,
            "ps",
            "--filter",
            f"label=swarmcloud.network_id={network_id}",
            "--filter",
            "status=running",
            "-q",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        return 0
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _resolve_bind_source_path(source_path):
    """
    Resolve a bind-mount source path so it is valid for the Docker daemon host.
    In containerized deployments, app code paths (e.g. /app/...) may differ from
    host paths, so we remap via HOST_PROJECT_PATH when available.
    """
    abs_source = os.path.abspath(source_path)
    host_project_path = os.getenv("HOST_PROJECT_PATH", "").strip()
    if not host_project_path:
        return abs_source

    try:
        relative = os.path.relpath(abs_source, settings.BASE_DIR)
    except Exception:
        return abs_source

    if relative.startswith(".."):
        return abs_source

    return os.path.abspath(os.path.join(host_project_path, relative))


def _docker_container_exists(docker_path, container_name, env):
    result = subprocess.run(  # nosec B603
        [docker_path, "inspect", container_name],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result.returncode == 0


@shared_task(bind=True)
def execute_and_log_in_container(
    self, container_name, command, network_id, project_id, user_id
):
    """
    Executes a shell command inside a running Docker container and
    streams its output (STDOUT/STDERR) directly to the database log entries.
    """
    try:
        network = SwarmNetwork.objects.get(identifier=network_id)
        project = Project.objects.get(identifier=project_id)
        logger = get_logger(project=project)

        # Connect to local Docker daemon
        client = docker.from_env()
        container = client.containers.get(container_name)

        logger.network.info(f"Executing in {container_name}: {command}")

        # Execute the command and stream the results line-by-line
        exec_result = container.exec_run(command, stream=True)

        for line in exec_result.output:
            # Create a database log entry for each line produced by the
            # container
            LogEntry.objects.create(
                user_id=user_id,
                project=project,
                swarm_network=network,
                category=LogCategory.NETWORK,
                source=container_name,
                level="INFO",
                message=line.decode("utf-8").strip(),
            )

    except Exception as e:
        # Log failure if container or command execution fails
        internal_logger = get_logger()
        internal_logger.network.error(f"Exec failed in {container_name}: {e}")


@shared_task
def start_swarm_network_task(network_id, user_id):
    """
    Deploys a swarm network using NVFlare containerized deployment.
    """
    try:
        swarm_network = SwarmNetwork.objects.get(identifier=network_id)
        user = User.objects.get(id=user_id)
        logger = get_logger(user=user, project=swarm_network.project)

        project_name = slugify(swarm_network.project.title).replace("-", "_")
        provision_dir = os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(swarm_network.project.identifier),
            str(swarm_network.identifier),
        )
        base_prod_path = os.path.join(
            provision_dir, "workspace", project_name, "prod_00"
        )

        targets = _discover_runtime_targets(base_prod_path)
        if not targets:
            raise FileNotFoundError(
                f"No NVFlare startup kits found under: {base_prod_path}"
            )

        _ensure_runtime_requirements_file(
            swarm_network=swarm_network,
            provision_dir=provision_dir,
            base_prod_path=base_prod_path,
            logger=logger,
        )

        logger.network.info(
            f"Discovered startup kits for roles: {[t['role'] for t in targets]}"
        )

        docker_path = shutil.which("docker") or "docker"
        env = os.environ.copy()

        configured_image = os.getenv("SWARMCLOUD_FLARE_IMAGE", "").strip()
        force_configured_image = (
            os.getenv("SWARMCLOUD_FORCE_CONFIGURED_IMAGE", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        has_custom_requirements = _has_custom_runtime_requirements(
            provision_dir=provision_dir,
            base_prod_path=base_prod_path,
        )

        use_configured_image = bool(configured_image)
        if configured_image and has_custom_requirements and not force_configured_image:
            logger.network.warning(
                "Custom runtime requirements detected; ignoring SWARMCLOUD_FLARE_IMAGE "
                "and building a project-specific runtime image. "
                "Set SWARMCLOUD_FORCE_CONFIGURED_IMAGE=true to override."
            )
            use_configured_image = False

        if use_configured_image:
            image_name = configured_image
        else:
            image_name = (
                f"swarmcloud_nvflare_{str(swarm_network.identifier)[:12]}:2.7.1"
            )
            docker_build_dir = os.path.join(base_prod_path, "nvflare")
            if os.path.exists(os.path.join(docker_build_dir, "Dockerfile")):
                logger.network.info(
                    f"Building runtime image for network: {image_name}"
                )
                _build_image_with_compat(
                    docker_path=docker_path,
                    image_name=image_name,
                    cwd=docker_build_dir,
                    env=env,
                    logger=logger,
                    error_command="docker build",
                )
            else:
                _build_local_fallback_image(
                    docker_path=docker_path,
                    env=env,
                    logger=logger,
                    image_name=image_name,
                    provision_dir=provision_dir,
                    base_prod_path=base_prod_path,
                )

        network_name = _docker_network_name(swarm_network.identifier)
        _ensure_docker_network(docker_path, network_name, env, logger)

        server_only_mode = (
            os.getenv("SWARMCLOUD_SERVER_ONLY_MODE", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )

        has_server_target = any(t["role"] == "server" for t in targets)
        filtered_targets = []
        for target in targets:
            if server_only_mode and target["role"] == "client":
                continue
            filtered_targets.append(target)

        if not server_only_mode and has_server_target:
            client_targets = [
                t for t in filtered_targets if t.get("role") == "client"
            ]
            if len(client_targets) > 1:
                project_yml_path = os.path.join(provision_dir, "project.yml")
                local_client_names = _resolve_local_client_names(
                    project_yml_path, base_prod_path
                )
                matched_local_clients = [
                    t for t in client_targets if t.get("name") in local_client_names
                ]

                if matched_local_clients:
                    allowed_local_names = {
                        t.get("name") for t in matched_local_clients
                    }
                    filtered_targets = [
                        t
                        for t in filtered_targets
                        if t.get("role") != "client"
                        or t.get("name") in allowed_local_names
                    ]
                    logger.network.info(
                        "Detected full startup bundle; limiting client startup "
                        f"to local participant(s): {sorted(allowed_local_names)}"
                    )

        logger.network.info(
            f"Starting containerized runtime (server_only_mode={server_only_mode})"
        )

        for target in filtered_targets:
            startup_dir = target["startup_dir"]
            role = target["role"]
            participant_name = target["name"]
            host_workspace_path = _resolve_bind_source_path(target["path"])

            host_startup_dir = os.path.join(host_workspace_path, "startup")
            runtime_startup_dir = "/workspace/startup"
            mount_mode = "bind"
            shared_container_name = (
                os.getenv("SWARMCLOUD_APP_CONTAINER", "swarmcloud").strip()
                or "swarmcloud"
            )

            if not os.path.isdir(host_startup_dir):
                if _docker_container_exists(
                    docker_path=docker_path,
                    container_name=shared_container_name,
                    env=env,
                ):
                    mount_mode = "volumes-from"
                    runtime_startup_dir = os.path.join(
                        target["path"], "startup"
                    )
                    logger.network.info(
                        "Startup workspace not found on host bind path; "
                        f"using --volumes-from {shared_container_name} for {participant_name}."
                    )
                else:
                    raise RuntimeError(
                        "Resolved Docker bind mount path does not contain startup directory: "
                        f"{host_startup_dir}. "
                        "Set HOST_PROJECT_PATH to the host path of this repository, "
                        "or set SWARMCLOUD_APP_CONTAINER to a running container "
                        "with /app/workspaces mounted."
                    )

            if role in {"client", "server"}:
                # Clear stale PID marker files from previous runs.
                # These files live one level above startup and can cause
                # start/sub_start scripts to exit early with
                # "There seems to be one instance ... running".
                for pid_file in ("pid.fl", "daemon_pid.fl"):
                    for base_dir in (target["path"], host_workspace_path):
                        try:
                            candidate = os.path.join(base_dir, pid_file)
                            if os.path.exists(candidate):
                                os.remove(candidate)
                                logger.network.info(
                                    f"Removed stale runtime marker: {candidate}"
                                )
                        except Exception as e:
                            logger.network.warning(
                                f"Could not remove stale runtime marker {pid_file}: {e}"
                            )

                _ensure_executable(os.path.join(startup_dir, "sub_start.sh"))
                command = [
                    "/bin/bash",
                    "-lc",
                    f"cd {runtime_startup_dir} && ./sub_start.sh",
                ]
            else:
                _ensure_executable(os.path.join(startup_dir, "start.sh"))
                command = [
                    "/bin/bash",
                    "-lc",
                    f"cd {runtime_startup_dir} && ./start.sh",
                ]

            container_name = _container_name_for(
                swarm_network.identifier, participant_name
            )

            subprocess.run(  # nosec B603
                [docker_path, "rm", "-f", container_name],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )

            client_host_network_enabled = (
                os.getenv("SWARMCLOUD_CLIENT_HOST_NETWORK", "true")
                .strip()
                .lower()
                in {"1", "true", "yes", "on"}
            )
            use_host_network = role == "client" and client_host_network_enabled

            run_cmd = [
                docker_path,
                "run",
                "-d",
                "--name",
                container_name,
                "--label",
                f"swarmcloud.network_id={swarm_network.identifier}",
                "--label",
                f"swarmcloud.role={role}",
                "-e",
                "GRPC_ENABLE_FORK_SUPPORT=0",
                "-e",
                "NVFLARE_START_METHOD=spawn",
                "-e",
                f"SWARMCLOUD_PROJECT_ID={str(swarm_network.project.identifier)}",
                "-e",
                f"AWS_ACCESS_KEY_ID={settings.AWS_ACCESS_KEY_ID}",
                "-e",
                f"AWS_SECRET_ACCESS_KEY={settings.AWS_SECRET_ACCESS_KEY}",
                "-e",
                f"AWS_S3_REGION_NAME={settings.AWS_S3_REGION_NAME}",
                "-e",
                f"AWS_STORAGE_BUCKET_NAME={settings.AWS_STORAGE_BUCKET_NAME}",
                "-e",
                "SWARMCLOUD_USE_LOCAL_DATA=1",
            ]

            if use_host_network:
                run_cmd.extend(["--network", "host"])
                logger.network.info(
                    f"Using host network mode for client container {participant_name}"
                )
            else:
                run_cmd.extend(
                    [
                        "--network",
                        network_name,
                        "--network-alias",
                        participant_name,
                    ]
                )

            if mount_mode == "bind":
                run_cmd.extend(["-v", f"{host_workspace_path}:/workspace"])
            else:
                run_cmd.extend(["--volumes-from", shared_container_name])

            if role == "server":
                fed_server_json = _load_json_file(
                    os.path.join(startup_dir, "fed_server.json")
                )
                server_def = (fed_server_json.get("servers") or [{}])[0]
                fed_learn_port = 8002
                admin_port = 8003
                try:
                    service_target = (
                        server_def.get("service", {})
                        .get("target", "server:8002")
                        .split(":")[-1]
                    )
                    fed_learn_port = int(service_target)
                    admin_port = int(server_def.get("admin_port", 8003))
                except Exception:
                    pass

                persist_dir = os.path.join(target["path"], ".nvflare_persist")
                os.makedirs(persist_dir, exist_ok=True)
                if mount_mode == "bind":
                    host_persist_dir = _resolve_bind_source_path(persist_dir)
                    run_cmd.extend(
                        [
                            "-v",
                            f"{host_persist_dir}:/tmp/nvflare",
                        ]
                    )
                _add_required_port_mapping(
                    run_cmd=run_cmd,
                    container_port=fed_learn_port,
                    logger=logger,
                    participant_name=participant_name,
                )
                _add_required_port_mapping(
                    run_cmd=run_cmd,
                    container_port=admin_port,
                    logger=logger,
                    participant_name=participant_name,
                )

            if role == "client":
                local_s3_endpoint = os.getenv("SWARMCLOUD_LOCAL_S3_ENDPOINT", "").strip()
                if not local_s3_endpoint:
                    configured_endpoint = (
                        os.getenv("AWS_S3_ENDPOINT_URL", "").strip()
                        or getattr(settings, "AWS_S3_ENDPOINT_URL", "")
                    )
                    if use_host_network:
                        local_s3_endpoint = (
                            _endpoint_with_localhost(configured_endpoint)
                            or "http://127.0.0.1:9000"
                        )
                    else:
                        local_s3_endpoint = configured_endpoint

                if local_s3_endpoint:
                    run_cmd.extend([
                        "-e",
                        f"AWS_S3_ENDPOINT_URL={local_s3_endpoint}",
                        "-e",
                        f"SWARMCLOUD_LOCAL_S3_ENDPOINT={local_s3_endpoint}",
                    ])

                if has_server_target:
                    # On nodes that also run the FL server, host-networked clients
                    # should resolve "server" and "minio" to the local host interface.
                    run_cmd.extend(["--add-host", "server:127.0.0.1"])
                    run_cmd.extend(["--add-host", "minio:127.0.0.1"])
                else:
                    remote_host = (
                        os.getenv("SWARMCLOUD_SERVER_HOST", "").strip()
                        or (Path(startup_dir) / "server_host.txt").read_text().strip()
                        if os.path.exists(os.path.join(startup_dir, "server_host.txt"))
                        else ""
                    )
                    if not remote_host:
                        remote_host = _extract_host_from_server_endpoint(startup_dir)
                    if remote_host:
                        run_cmd.extend(["--add-host", f"server:{remote_host}"])
                        run_cmd.extend(["--add-host", f"minio:{remote_host}"])

                        aliases_file = os.path.join(startup_dir, "server_aliases.txt")
                        if os.path.exists(aliases_file):
                            try:
                                with open(aliases_file) as af:
                                    for alias in af.read().splitlines():
                                        alias = alias.strip()
                                        if alias:
                                            run_cmd.extend(
                                                [
                                                    "--add-host",
                                                    f"{alias}:{remote_host}",
                                                ]
                                            )
                            except Exception:
                                pass

            run_cmd.extend([image_name] + command)

            logger.network.info(
                f"Starting {role} container: {container_name}"
            )
            run_result = subprocess.run(  # nosec B603
                run_cmd,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            if run_result.returncode != 0:
                raise RuntimeError(
                    f"Failed to start {container_name}: {run_result.stderr.strip()}"
                )

            # Give the process a moment to initialize and verify it is still running.
            time.sleep(1)
            if not _is_container_running(docker_path, container_name, env):
                container_logs = _get_container_last_logs(
                    docker_path=docker_path,
                    container_name=container_name,
                    env=env,
                    lines=60,
                )
                raise RuntimeError(
                    f"Container {container_name} exited during startup. Logs:\n{container_logs}"
                )

        try:
            logger.network.info(
                f"Connecting app and storage to network: {network_name}"
            )
            subprocess.run(  # nosec B603
                [docker_path, "network", "connect", network_name, "swarmcloud"],
                capture_output=True,
                env=env,
                check=False,
            )
            subprocess.run(  # nosec B603
                [docker_path, "network", "connect", network_name, "minio"],
                capture_output=True,
                env=env,
                check=False,
            )
        except Exception as e:
            logger.network.warning(
                f"Could not connect containers to FLARE network: {e}"
            )

        running_count = _count_running_labeled_containers(
            network_id=swarm_network.identifier,
            docker_path=docker_path,
            env=env,
        )
        if running_count == 0:
            raise RuntimeError(
                "No FLARE runtime containers are running after startup."
            )

        # Mark as running only after verifying at least one runtime container is alive.
        swarm_network.status = "RUNNING"
        swarm_network.save()
        run_nvflare_preflight_check.delay(network_id, user_id)

    except Exception as e:
        if "swarm_network" in locals():
            swarm_network.status = "ERROR"
            swarm_network.save()

        # Ensure the error is logged to the database so the user sees it
        internal_logger = get_logger(
            user=User.objects.get(id=user_id),
            project=(
                swarm_network.project if "swarm_network" in locals() else None
            ),
        )
        internal_logger.network.error(f"Failed to start network: {str(e)}")


@shared_task
def run_nvflare_preflight_check(network_id, user_id):
    """
    Executes the NVFlare preflight check utility using the admin startup kit.
    """
    try:
        network = SwarmNetwork.objects.get(identifier=network_id)
        user = User.objects.get(id=user_id)
        logger = get_logger(user=user, project=network.project)

        project_name = slugify(network.project.title).replace("-", "_")
        provision_dir = os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(network.project.identifier),
            str(network.identifier),
        )
        admin_startup_dir = os.path.join(
            provision_dir,
            "workspace",
            project_name,
            "prod_00",
            "admin@nvidia.com",
            "startup",
        )

        if not os.path.exists(admin_startup_dir):
            prod_dir = os.path.join(
                provision_dir,
                "workspace",
                project_name,
                "prod_00",
            )
            if os.path.exists(prod_dir):
                admin_dirs = sorted(
                    [
                        os.path.join(prod_dir, d, "startup")
                        for d in os.listdir(prod_dir)
                        if d.startswith("admin-")
                        and os.path.isdir(os.path.join(prod_dir, d, "startup"))
                    ]
                )
                if admin_dirs:
                    admin_startup_dir = admin_dirs[0]

        if not os.path.exists(admin_startup_dir):
            logger.network.info(
                "Skipping Preflight Check: Admin startup kit not found (Expected for Client Nodes)."
            )
            return

        logger.network.info("Running NVFlare Preflight Check...")

        python_path = shutil.which("python3") or "python3"
        command = [
            python_path,
            "-m",
            "nvflare.tool.preflight_check",
            "-p",
            admin_startup_dir,
        ]

        env = os.environ.copy()
        result = subprocess.run(
            command, capture_output=True, text=True, env=env
        )  # nosec B603

        for output in [result.stdout, result.stderr]:
            for line in output.splitlines():
                if line.strip():
                    LogEntry.objects.create(
                        user=user,
                        project=network.project,
                        swarm_network=network,
                        category=LogCategory.NETWORK,
                        source="preflight-check",
                        level="INFO" if output == result.stdout else "ERROR",
                        message=line.strip(),
                    )

        if result.returncode == 0:
            logger.network.info("Preflight check passed.")
        else:
            logger.network.warning("Preflight check found issues. Check logs.")

    except Exception as e:
        print(f"Preflight exception: {e}")


@shared_task
def stop_swarm_network_task(network_id, user_id):
    """
    Stops and removes the containers for a swarm network.
    """
    try:
        network = SwarmNetwork.objects.get(identifier=network_id)
        user = User.objects.get(id=user_id)
        logger = get_logger(user=user, project=network.project)

        logger.network.info(f"Stopping network: {network.name}")
        docker_path = shutil.which("docker") or "docker"
        env = os.environ.copy()

        _stop_labeled_runtime(network.identifier, docker_path, env, logger)

        network.status = "STOPPED"
        network.save()
        logger.network.info("Network stopped successfully.")

    except Exception as e:
        print(f"Failed to stop network: {e}")


@shared_task
def stop_and_delete_network_task(network_id):
    """
    Stops a running network and then deletes its database record.
    This ensures that resources are cleaned up before the record is gone.
    """
    try:
        network = SwarmNetwork.objects.get(identifier=network_id)

        # 1. Perform cleanup (stops containers, syncs results, and removes files)
        # We call the cleanup function synchronously within this task
        cleanup_network_resources(
            project_title=network.project.title,
            project_identifier=str(network.project.identifier),
            network_identifier=str(network.identifier),
            network_name=network.name,
        )

        # 2. Delete the record from the database
        # We use filter().delete() to avoid re-triggering the custom delete() method
        SwarmNetwork.objects.filter(identifier=network_id).delete()

    except SwarmNetwork.DoesNotExist:
        # If it was already deleted, we just ensure resources are gone
        # (Though we don't have the details here, cleanup_network_resources
        # might have already been called by another task)
        pass
    except Exception as e:
        internal_logger = get_logger()
        internal_logger.network.error(
            f"Failed to stop and delete network {network_id}: {e}"
        )


@shared_task
def broadcast_all_network_statuses():
    """
    Recurring task to trigger Gossip broadcasts for all active networks.
    Ensures peer-to-peer status visibility.
    """
    from .views import broadcast_network_status
    active_networks = SwarmNetwork.objects.filter(status="RUNNING")
    
    if not active_networks.exists():
        return

    from logs.logger import get_logger
    logger = get_logger()
    logger.network.debug(f"[CELERY BEAT] Triggering gossip broadcast for {active_networks.count()} active network(s).")

    for network in active_networks:
        try:
            broadcast_network_status(network.identifier)
        except Exception as e:
            logger.network.error(f"[CELERY BEAT] Gossip broadcast failed for {network.name}: {e}")


@shared_task
def cleanup_network_resources(
    project_title, project_identifier, network_identifier, network_name
):
    """
    Asynchronously cleans up Docker containers and filesystem resources
    associated with a deleted SwarmNetwork.

    Arguments are passed as strings since the DB record might already be deleted.
    """
    try:
        # Use a generic system logger since the network/project might be gone
        logger = get_logger()

        provision_dir = os.path.join(
            settings.BASE_DIR,
            "workspaces",
            str(project_identifier),
            str(network_identifier),
        )

        # 1. Stop Docker containers started by SwarmCloud runtime
        docker_bin = shutil.which("docker") or "docker"
        env = os.environ.copy()
        try:
            _stop_labeled_runtime(network_identifier, docker_bin, env, logger)
        except Exception as e:
            logger.network.error(
                f"Failed to stop Docker containers for deleted network {network_name}: {e}"
            )

        # 2. Sync results to S3 before wiping the local filesystem
        try:
            from results.tasks import sync_project_results

            logger.network.info(
                f"Synchronizing results for network {network_name} before deletion."
            )
            # Run synchronously to ensure files are uploaded before rmtree
            sync_project_results(project_identifier)
        except Exception as e:
            logger.network.error(
                f"Result sync failed during network cleanup for {network_name}: {e}"
            )

        # 3. Cleanup Filesystem
        if os.path.exists(provision_dir):
            shutil.rmtree(provision_dir)
            logger.network.info(
                f"Cleaned up workspace directory for deleted network: {network_name}"
            )

    except Exception as e:
        logger.network.error(
            f"Error during network resource cleanup for {network_name}: {e}"
        )
