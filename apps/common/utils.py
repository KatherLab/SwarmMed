"""
Shared utility functions for the entire application.
"""

import boto3
from urllib.parse import urlparse
from django.conf import settings
from django.http import JsonResponse
from django.utils.http import url_has_allowed_host_and_scheme


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
