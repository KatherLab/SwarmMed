"""
Utility functions for the users application.
Includes helper functions for filtering user lists based on request parameters.
"""

from urllib.parse import urlparse

from django.utils.http import url_has_allowed_host_and_scheme


def user_filter(request):
    """
    Parses the GET parameters from an HTTP request and converts them into
    a dictionary of Django database filters.

    Currently supports:
    - search: Filters by username (case-insensitive contains).
    """
    filter_dict = {}

    # Mapping of frontend parameter names to backend Django filter keys.
    filter_mappings = {"search": "username__icontains"}

    # Iterate through all parameters in the URL (e.g., ?search=alice&page=2).
    for key in request.GET:
        # We only care about keys that are in our mapping and have a non-empty value.
        # We skip 'page' as it's used for pagination, not database filtering.
        if key in filter_mappings and request.GET.get(key) and key != "page":
            db_filter_key = filter_mappings[key]
            filter_dict[db_filter_key] = request.GET.get(key)

    return filter_dict


def get_safe_referer(request, default="/"):
    """
    Returns a safe referer URL or a default path if the referer is missing
    or potentially malicious (open redirect).
    """
    referer = request.META.get("HTTP_REFERER")
    if not referer:
        return default

    # Check if the referer is safe (same host and scheme).
    # This check relies on Django's built-in validation to prevent
    # Open Redirect attacks.
    is_safe = url_has_allowed_host_and_scheme(
        url=referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    )

    if is_safe:
        # Extract path and query to ensure we stay on the same domain
        parsed = urlparse(referer)
        safe_url = parsed.path

        # Sanitization: Ensure path starts with / but not // (protocol relative)
        if not safe_url.startswith("/"):
            safe_url = "/" + safe_url
        
        # Explicitly prevent // at start which could be interpreted as
        # protocol-relative URL by some browsers/libraries
        while safe_url.startswith("//"):
            safe_url = safe_url[1:]

        # Strip control characters to prevent header injection
        safe_url = safe_url.replace("\r", "").replace("\n", "")

        if parsed.query:
            safe_url += f"?{parsed.query}"
        
        return safe_url

    return default
