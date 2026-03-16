"""
Custom Logging Handlers for the logs app.
Defines a handler that writes log records directly into the Django database.
"""

import logging
import os

from .context import _thread_locals
from .utils import redact_message, redact_phi

_internal_logger = logging.getLogger("app")


class DatabaseLogHandler(logging.Handler):
    """
    A custom logging handler that redirects Python logging output
    into the database as LogEntry records.
    """

    def emit(self, record):
        """
        Process a single log record and offload it to a Celery task
        for asynchronous database storage.
        """
        try:
            # 1. Extract context from record and thread-locals
            user_id = getattr(record, "user_id", None)
            project_id = getattr(record, "project_id", None)
            swarm_network_id = getattr(record, "swarm_network_id", None)
            object_id = getattr(record, "object_id", None)
            category = getattr(record, "category", "project")

            request = getattr(_thread_locals, "request", None)
            ip_address = None
            user_agent = None
            path = None

            if request:
                x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
                if x_forwarded_for:
                    ip_address = x_forwarded_for.split(",")[0].strip()
                else:
                    ip_address = request.META.get("REMOTE_ADDR")

                user_agent = request.META.get("HTTP_USER_AGENT")
                path = request.path

            # 2. Redact sensitive info
            safe_message = redact_message(record.getMessage())
            context_data = getattr(record, "context_data", {})
            safe_context_data = redact_phi(context_data)

            # 3. Determine source
            source = getattr(record, "source", "web")
            if os.environ.get("CELERY_WORKER"):
                source = "celery"
            elif os.environ.get("CONTAINER_NAME"):
                source = os.environ.get("CONTAINER_NAME")

            # 4. Prepare data for the async task
            # We pass a dictionary of serializable data
            log_data = {
                "category": category,
                "level": record.levelname,
                "message": safe_message,
                "context_data": safe_context_data,
                "ip_address": ip_address,
                "user_agent": user_agent,
                "path": path,
                "object_id": object_id,
                "source": source,
                "user_id": user_id,
                "project_id": project_id,
                "swarm_network_id": swarm_network_id,
            }

            # 5. Trigger the Celery task
            # We import here to avoid circular dependencies
            from .tasks import async_save_log_task
            async_save_log_task.delay(log_data)

        except Exception as e:
            # A failure in logging should NEVER crash the main application.
            _internal_logger.debug(f"Async database logging failed to enqueue: {e}")
