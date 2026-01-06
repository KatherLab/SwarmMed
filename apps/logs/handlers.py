"""
Custom Logging Handlers for the logs app.
Defines a handler that writes log records directly into the Django database.
"""

import logging

from .utils import redact_message, redact_phi

_internal_logger = logging.getLogger("app")


class DatabaseLogHandler(logging.Handler):
    """
    A custom logging handler that redirects Python logging output
    into the database as LogEntry records.
    """

    def emit(self, record):
        """
        Process a single log record and save it to the database.
        """
        try:
            # Extract user and project context if available.
            # We log even if these are missing to ensure a complete audit trail.
            user_id = getattr(record, "user_id", None)
            project_id = getattr(record, "project_id", None)
            object_id = getattr(record, "object_id", None)

            from django.apps import apps

            from .context import _thread_locals

            # Use dynamic model loading to avoid circular imports during
            # startup
            try:
                log_entry_model = apps.get_model("logs", "LogEntry")
                project_model = apps.get_model("project", "Project")
            except (LookupError, RuntimeError):
                # Models might not be ready during early initialization
                return

            # Extract request metadata from thread-local storage
            request = getattr(_thread_locals, "request", None)
            ip_address = None
            user_agent = None
            path = None

            if request:
                # IP Address handling (accounting for proxies)
                x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
                if x_forwarded_for:
                    ip_address = x_forwarded_for.split(",")[0].strip()
                else:
                    ip_address = request.META.get("REMOTE_ADDR")

                user_agent = request.META.get("HTTP_USER_AGENT")
                path = request.path

            # Apply PHI/PII redaction to message and context data
            safe_message = redact_message(record.getMessage())
            context_data = getattr(record, "context_data", {})
            safe_context_data = redact_phi(context_data)

            # Determine source (e.g. 'web', 'celery')
            # If we are in a celery worker, the process name or a specific env var might tell us.
            source = getattr(record, "source", "web")

            # Check for common environment indicators
            import os

            if os.environ.get("CELERY_WORKER"):
                source = "celery"
            elif os.environ.get("CONTAINER_NAME"):
                source = os.environ.get("CONTAINER_NAME")

            # Create the database record
            # Optimization: Assign IDs directly to avoid extra SELECT queries
            log_entry = log_entry_model(
                category=getattr(record, "category", "project"),
                level=record.levelname,
                message=safe_message,
                context_data=safe_context_data,
                ip_address=ip_address,
                user_agent=user_agent,
                path=path,
                object_id=object_id,
                source=source,
            )

            if user_id:
                log_entry.user_id = user_id

            if project_id:
                # project_id in record might be the 'identifier' (UUID) or PK.
                # Project model uses identifier (UUID) as a unique field, but PK is an integer.
                # LogEntry.project is a ForeignKey to Project.
                # If project_id is a UUID, we still need to find the PK if we want to avoid the SELECT.
                # But wait, LogEntry.project is defined as:
                # project = models.ForeignKey('project.Project', ...)
                # In Django, if we have the PK, we can do log_entry.project_id = pk.
                # Since Project identifier is unique but NOT the PK, we have to look it up
                # unless we change the record to pass the PK.
                # For safety, if it's a UUID (identifier), we do one lookup.
                try:
                    # If it's a project instance already, take the PK
                    if hasattr(project_id, "pk"):
                        log_entry.project_id = project_id.pk
                    else:
                        project = (
                            project_model.objects.filter(identifier=project_id)
                            .only("id")
                            .first()
                        )
                        if project:
                            log_entry.project_id = project.id
                except Exception as e:
                    _internal_logger.debug(
                        f"Could not resolve project_id for logging: {e}"
                    )

            log_entry.save()

        except Exception as e:
            # CRITICAL: A failure in logging should NEVER crash the main application.
            # We catch all exceptions and fail gracefully, logging the error
            # to the internal console logger.
            _internal_logger.debug(f"Database logging failed: {e}")
