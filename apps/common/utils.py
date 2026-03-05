"""
Shared utility functions for the entire application.
"""

import os
from urllib.parse import urlparse

import boto3
import docker
from django.conf import settings
from django.http import JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify


def get_docker_client(target="host"):
    """
    Returns a Docker client configured for a specific target.
    'host' -> talk to the host via docker-proxy (Infra/Flare)
    'sandbox' -> talk to sandbox-dind (User scripts)
    """
    # Temporarily remove these to avoid interference
    cert_file = os.environ.pop("SSL_CERT_FILE", None)
    ca_bundle = os.environ.pop("REQUESTS_CA_BUNDLE", None)

    try:
        if target == "sandbox":
            # Explicitly target the DIND daemon with its TLS certs
            client = docker.DockerClient(
                base_url="tcp://sandbox-dind:2376",
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
    """
    Translates a path inside the container to its absolute path on the host.
    Required for Docker volume mounting when running in a DIND environment.
    """
    host_project_path = os.getenv("HOST_PROJECT_PATH")
    if not host_project_path:
        return container_path

    rel_path = os.path.relpath(container_path, settings.BASE_DIR)
    host_path = os.path.join(host_project_path, rel_path)
    return host_path.replace("\\", "/")


def get_s3_client():
    """
    Creates and returns an S3 client using the internal network credentials.
    This client uses the internal endpoint URL (useful for server-to-server).
    """
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
        endpoint_url=settings.AWS_S3_ENDPOINT_URL,
    )


def get_public_s3_client():
    """
    Creates and returns an S3 client using the public URL settings.
    This is used for generating presigned URLs that work in the user's browser.
    """
    return boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION_NAME,
        endpoint_url=settings.PUBLIC_URL,
    )


def format_size(size_bytes):
    """
    Converts a number of bytes into a human-readable string (e.g., '1.2 MB').
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
    """
    Generates a temporary presigned URL for downloading an S3 object.
    """
    s3 = get_public_s3_client()
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.AWS_STORAGE_BUCKET_NAME, "Key": key},
        ExpiresIn=expires,
    )
    return url


def get_internal_s3_download_url(key, expires=3600):
    """
    Generates a temporary presigned URL for internal use within the Docker network.
    Ensures that the host in the URL is reachable from other containers (uses 'minio').
    """
    s3 = get_s3_client()
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.AWS_STORAGE_BUCKET_NAME, "Key": key},
        ExpiresIn=expires,
    )

    # If the URL contains localhost or 127.0.0.1, other containers won't be able
    # to reach it. We replace it with the internal service name 'minio'.
    if "localhost" in url:
        url = url.replace("localhost", "minio")
    elif "127.0.0.1" in url:
        url = url.replace("127.0.0.1", "minio")

    return url


def get_safe_referer(request, default="/"):
    """
    Returns a safe referer URL or a default path if the referer is missing
    or potentially malicious (open redirect).
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
    """
    Returns a standardized JSON success response.
    """
    payload = {"status": "success"}
    if data is not None:
        payload["data"] = data
    if message is not None:
        payload["message"] = message
    return JsonResponse(payload, status=status)


def api_error(message, errors=None, status=400):
    """
    Returns a standardized JSON error response.
    """
    payload = {"status": "error", "message": message}
    if errors is not None:
        payload["errors"] = errors
    return JsonResponse(payload, status=status)


def get_safe_slug(source_value, fallback):
    """Return a filesystem-safe slug, falling back to provided identifier."""
    slug = slugify(source_value or "")
    if not slug:
        slug = str(fallback)
    return slug
