"""Django bootstrap helpers for the swarmed CLI."""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess  # nosec B404
import sys
from pathlib import Path

from dotenv import load_dotenv


def _can_resolve_host(hostname: str) -> bool:
    """Return True if ``hostname`` resolves via system DNS/hosts."""
    try:
        socket.gethostbyname(hostname)
        return True
    except OSError:
        return False


def _is_running_in_container() -> bool:
    """Return True when executing inside a Docker/OCI container."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _discover_published_host_port(container_name: str, container_port: int) -> str:
    """Return the published host port for a container port via ``docker port``, or empty string."""
    try:
        result = subprocess.run(  # nosec B603 B607
            ["docker", "port", container_name, f"{container_port}/tcp"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            first_line = result.stdout.strip().splitlines()[0].strip()
            if ":" in first_line:
                port = first_line.rsplit(":", 1)[-1].strip()
                if port.isdigit():
                    return port
    except Exception:
        pass
    return ""


def _configure_host_cli_environment(
    repo_root: Path,
    env: dict[str, str] | None = None,
    *,
    in_container: bool | None = None,
) -> None:
    """Translate Docker-internal hostnames when running the CLI on the host OS.

    The generated ``.env`` file uses container service names (``postgres``,
    ``redis``, ``https://minio:9000``) and container CA paths. When an operator
    runs ``swarmed`` directly on the host (Linux, macOS, or Windows), fall back
    to the published host endpoints and local ``.secrets`` CA bundle whenever
    the internal service names are not resolvable.
    """
    target_env = os.environ if env is None else env
    if in_container is None:
        in_container = _is_running_in_container()
    if in_container:
        return

    target_env.setdefault("SWARMMEDHUB_SKIP_STARTUP_BUCKET_CHECK", "1")

    host_ca = repo_root / ".secrets" / "certs" / "internal" / "ca.crt"
    if host_ca.exists():
        for cert_var in (
            "CA_CERT_PATH",
            "DB_SSLROOTCERT",
            "REQUESTS_CA_BUNDLE",
            "SSL_CERT_FILE",
            "AWS_CA_BUNDLE",
        ):
            current = target_env.get(cert_var, "").strip()
            if not current or not Path(current).exists():
                target_env[cert_var] = str(host_ca)

    db_host = target_env.get("DB_HOST", "").strip()
    if db_host in {"postgres", "pgbouncer"} and not _can_resolve_host(db_host):
        target_env["DB_HOST"] = "127.0.0.1"
        host_port = target_env.get("POSTGRES_HOST_PORT", "").strip()
        if not host_port:
            host_port = _discover_published_host_port("postgres", 5432)
        if host_port:
            target_env["DB_PORT"] = host_port

    redis_host = target_env.get("REDIS_HOST", "redis").strip() or "redis"
    if redis_host == "redis" and not _can_resolve_host("redis"):
        target_env["REDIS_HOST"] = "127.0.0.1"

    s3_endpoint = (
        target_env.get("AWS_S3_ENDPOINT_URL", "https://minio:9000").strip()
        or "https://minio:9000"
    )
    if s3_endpoint == "https://minio:9000" and not _can_resolve_host("minio"):
        docker_host_ip = (
            target_env.get("DOCKER_HOST_IP", "127.0.0.1").strip() or "127.0.0.1"
        )
        target_env["AWS_S3_ENDPOINT_URL"] = f"https://{docker_host_ip}:9000"
        if docker_host_ip not in {"127.0.0.1", "localhost"}:
            target_env.setdefault("SWARMMEDHUB_S3_VERIFY", "false")


def bootstrap_django() -> None:
    """Initialize Django with the same module path semantics as manage.py."""
    repo_root = Path(__file__).resolve().parent.parent
    apps_path = repo_root / "apps"

    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(apps_path) not in sys.path:
        sys.path.insert(0, str(apps_path))

    with contextlib.suppress(PermissionError, OSError):
        load_dotenv(repo_root / ".env", override=False)

    _configure_host_cli_environment(repo_root)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

    import django

    django.setup()
