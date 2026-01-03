"""
Audit Logging Middleware.
Automatically logs all data-modifying requests (POST, PUT, PATCH, DELETE)
to ensure a complete audit trail for HIPAA/GDPR compliance.
"""

import json
import logging
from .logger import get_logger
from .utils import redact_phi

# Internal logger for the middleware itself
_internal_logger = logging.getLogger("app")


class AuditLogMiddleware:
    """
    Middleware that captures and logs all data-modifying operations.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.logger = get_logger()

    def __call__(self, request):
        # We only care about data-modifying requests for the audit trail
        # GET, HEAD, OPTIONS are typically excluded from write-auditing but
        # might be logged separately for access-auditing if needed.
        is_write_request = request.method in ("POST", "PUT", "PATCH", "DELETE")

        # Get the response first to know if the request was successful
        response = self.get_response(request)

        if is_write_request:
            try:
                self._log_request(request, response)
            except Exception as e:
                # Middleware failures should NEVER crash the application
                _internal_logger.debug(f"Audit log middleware failed: {e}")

        return response

    def _log_request(self, request, response):
        """
        Prepares and saves a log entry for the current request.
        """
        # Skip certain paths that might be too noisy or sensitive (e.g., login passwords)
        # Note: REDACT_PHI already handles passwords, but skipping login POST might be preferred
        # if using django-axes which logs its own auth events.
        path = request.path
        if any(skip in path for skip in ["/login/", "/admin/jsi18n/"]):
            return

        user = request.user if request.user.is_authenticated else None

        # Don't log if status code is 404 or something irrelevant to data changes
        if response.status_code == 404:
            return

        # Prepare context data
        context = {
            "method": request.method,
            "path": path,
            "status_code": response.status_code,
            "query_params": dict(request.GET.items()),
        }

        # Attempt to capture some of the POST data (redacted)
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                if request.content_type == "application/json":
                    body = json.loads(request.body.decode("utf-8"))
                    context["payload"] = redact_phi(body)
                elif "multipart/form-data" not in request.content_type:
                    # For standard form data
                    payload = dict(request.POST.items())
                    context["payload"] = redact_phi(payload)
                else:
                    context["payload"] = "[MULTIPART_DATA_OMITTED]"
            except Exception:
                context["payload"] = "[COULD_NOT_PARSE_PAYLOAD]"

        # Determine the category based on the path
        category = "project"
        if "/data/" in path:
            category = "data"
        elif "/network/" in path:
            category = "network"
        elif "/training/" in path:
            category = "training"
        elif "/results/" in path:
            category = "results"
        # '/users/' and '/profile/' now fall under 'project' or can be explicitly mapped if needed.
        # But for now we stick to standard categories.

        # Log the message
        level = "INFO"
        if response.status_code >= 400:
            level = "WARNING"
        if response.status_code >= 500:
            level = "ERROR"

        message = f"Audit: {request.method} {path} - Status: {response.status_code}"

        from .logger import log

        log(level=level, message=message, category=category, user=user, **context)
