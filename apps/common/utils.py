"""Shared utility functions for the entire application.

This module provides various helper functions for Docker interactions, S3 storage,
API response formatting, and general utility tasks used across different apps.
"""

import os
import re
import socket
from urllib.parse import urlparse

import boto3
import docker
from django.conf import settings
from django.http import JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify


def parse_safe_requirement_lines(requirements_text):
    """Parses and sanitizes a requirements file content.

    Args:
        requirements_text (str): The raw text of the requirements file.

    Returns:
        list: A list of sanitized and unique requirement strings.
    """
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


def collect_project_runtime_requirements(project, logger):
    """Collects all runtime requirements for a project from local and remote sources.

    Args:
        project (Project): The project instance.
        logger (Logger): The logger instance for status updates.

    Returns:
        list: A consolidated list of requirement strings.
    """
    baseline = [
        "nvflare==2.7.1",
        "gunicorn==23.0.0",
        "boto3==1.34.100",
        "python-dotenv==1.0.1",
        "pandas==2.3.3",
        "numpy==2.4.3",
        "torch==2.10.0",
        "scikit-learn==1.8.0",
        "fsspec==2025.2.0",
        "aiohttp==3.13.3",
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
            lines = parse_safe_requirement_lines(text)
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


def get_docker_client(target="host"):
    """Returns a Docker client configured for a specific target.

    Args:
        target (str): The target environment. Can be 'host' to talk to the host via
            docker-proxy (Infra/Flare) or 'sandbox' to talk to sandbox-dind (User scripts).
            Defaults to "host".

    Returns:
        docker.DockerClient: A configured Docker client instance.
    """
    # Temporarily remove these to avoid interference
    cert_file = os.environ.pop("SSL_CERT_FILE", None)
    ca_bundle = os.environ.pop("REQUESTS_CA_BUNDLE", None)

    try:
        if target == "sandbox":
            # Explicitly target the DIND daemon with its TLS certs
            client = docker.DockerClient(
                base_url="tcp://sandbox-dind:2376",
                timeout=300,
                tls=docker.tls.TLSConfig(
                    client_cert=(
                        "/certs/client/cert.pem",
                        "/certs/client/key.pem",
                    ),
                    ca_cert="/certs/client/ca.pem",
                    verify=True,
                ),
            )
        else:
            # Default to DOCKER_HOST (which points to docker-proxy)
            client = docker.from_env()

        client.api.trust_env = False
        client.ping()
        return client
    finally:
        if cert_file is not None:
            os.environ["SSL_CERT_FILE"] = cert_file
        if ca_bundle is not None:
            os.environ["REQUESTS_CA_BUNDLE"] = ca_bundle


def get_host_path(container_path):
    """Translates a path inside the container to its absolute path on the host.

    Required for Docker volume mounting when running in a DIND environment.

    Args:
        container_path (str): The absolute path within the current container.

    Returns:
        str: The corresponding absolute path on the host system.
    """
    host_project_path = os.getenv("HOST_PROJECT_PATH")
    if not host_project_path:
        return container_path

    rel_path = os.path.relpath(container_path, settings.BASE_DIR)
    host_path = os.path.join(host_project_path, rel_path)
    return host_path.replace("\\", "/")


def get_s3_client():
    """Creates and returns an S3 client using the internal network credentials.

    This client uses the internal endpoint URL (useful for server-to-server).

    Returns:
        botocore.client.S3: A configured S3 client for internal use.
    """
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
    )


def get_public_s3_client():
    """Creates and returns an S3 client using the public URL settings.

    This is used for generating presigned URLs that work in the user's browser.

    Returns:
        botocore.client.S3: A configured S3 client for generating public URLs.
    """
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
        endpoint_url=settings.PUBLIC_URL,
    )


def format_size(size_bytes):
    """Converts a number of bytes into a human-readable string (e.g., '1.2 MB').

    Args:
        size_bytes (int): The number of bytes to format.

    Returns:
        str: A human-readable size string.
    """
    if size_bytes == 0:
        return "0 B"

    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    unit_index = 0

    while size > 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1

    if unit_index > 0:
        return f"{size:.1f} {units[unit_index]}"
    return f"{int(size)} {units[unit_index]}"


def get_s3_download_url(key, expires=3600):
    """Generates a temporary presigned URL for downloading an S3 object.

    Args:
        key (str): The S3 object key.
        expires (int): The number of seconds until the URL expires. Defaults to 3600.

    Returns:
        str: A presigned S3 download URL.
    """
    s3 = get_public_s3_client()
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.AWS_STORAGE_BUCKET_NAME, "Key": key},
        ExpiresIn=expires,
    )
    return url


def get_internal_s3_download_url(key, expires=3600):
    """Generates a temporary presigned URL for internal use within the Docker network.

    Ensures that the host in the URL is reachable from other containers.

    Args:
        key (str): The S3 object key.
        expires (int): The number of seconds until the URL expires. Defaults to 3600.

    Returns:
        str: A presigned S3 download URL resolvable within the internal network.
    """
    s3 = get_s3_client()
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.AWS_STORAGE_BUCKET_NAME, "Key": key},
        ExpiresIn=expires,
    )

    # If the URL contains localhost, 127.0.0.1 or 'minio', other containers or
    # remote nodes won't be able to reach it. We try to replace it with reachable candidates.
    internal_host = os.getenv("MEDSWARMHUB_SERVER_HOST", "").strip()
    if not internal_host:
        # 1. Try to resolve 'minio' (standard internal name)
        try:
            socket.gethostbyname("minio")
            internal_host = "minio"
        except (socket.gaierror, socket.herror):
            # 2. Try to extract host from PUBLIC_URL (e.g. Tailscale IP)
            public_url = getattr(settings, "PUBLIC_URL", "")
            if public_url:
                parsed_public = urlparse(public_url)
                if parsed_public.hostname and parsed_public.hostname not in {
                    "localhost",
                    "127.0.0.1",
                }:
                    internal_host = parsed_public.hostname

    if not internal_host:
        # 3. Check for host.docker.internal (macOS/Windows)
        try:
            socket.gethostbyname("host.docker.internal")
            internal_host = "host.docker.internal"
        except (socket.gaierror, socket.herror):
            pass

    if not internal_host:
        # Fallback to docker gateway (standard for Linux)
        internal_host = os.getenv("DOCKER_HOST_IP", "172.17.0.1")

    # Robustly replace all local host candidates with a resolvable hostname
    if "localhost" in url:
        url = url.replace("localhost", internal_host)
    if "127.0.0.1" in url:
        url = url.replace("127.0.0.1", internal_host)

    # Ensure 'minio' service name is used if internal_host was detected as something else
    # but the URL already points to minio (to avoid breaking existing working setups)
    if "://minio" in url and internal_host != "minio":
        url = url.replace("://minio", f"://{internal_host}")

    return url


def get_safe_referer(request, default="/"):
    """Returns a safe referer URL or a default path if the referer is missing or malicious.

    Prevents open redirect vulnerabilities by validating the referer's host and scheme.

    Args:
        request (HttpRequest): The incoming Django request object.
        default (str): The default path to return if the referer is unsafe. Defaults to "/".

    Returns:
        str: A validated safe referer path or the default.
    """
    referer = request.META.get("HTTP_REFERER")
    if not referer:
        return default

    # Check if the referer is safe (same host and scheme).
    is_safe = url_has_allowed_host_and_scheme(
        url=referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    )

    if is_safe:
        try:
            parsed = urlparse(referer)
            path = parsed.path

            if not path.startswith("/"):
                path = "/" + path

            while path.startswith("//"):
                path = path[1:]

            safe_url = path
            if parsed.query:
                safe_url += f"?{parsed.query}"

            return safe_url
        except Exception:
            return default

    return default


def api_success(data=None, message=None, status=200):
    """Returns a standardized JSON success response.

    Args:
        data (dict, optional): Data to include in the 'data' field of the response.
        message (str, optional): A descriptive message for the success.
        status (int): The HTTP status code. Defaults to 200.

    Returns:
        JsonResponse: A Django JSON response with status 'success'.
    """
    payload = {"status": "success"}
    if data is not None:
        payload["data"] = data
    if message is not None:
        payload["message"] = message
    return JsonResponse(payload, status=status)


def api_error(message, errors=None, status=400):
    """Returns a standardized JSON error response.

    Args:
        message (str): A descriptive error message.
        errors (dict, optional): Specific field errors or details.
        status (int): The HTTP status code. Defaults to 400.

    Returns:
        JsonResponse: A Django JSON response with status 'error'.
    """
    payload = {"status": "error", "message": message}
    if errors is not None:
        payload["errors"] = errors
    return JsonResponse(payload, status=status)


def get_safe_slug(source_value, fallback):
    """Returns a filesystem-safe slug, falling back to a provided identifier.

    Args:
        source_value (str): The value to slugify.
        fallback (Any): The fallback value if the slugified source is empty.

    Returns:
        str: A URL and filesystem-safe slug.
    """
    slug = slugify(source_value or "")
    if not slug:
        slug = str(fallback)
    return slug
