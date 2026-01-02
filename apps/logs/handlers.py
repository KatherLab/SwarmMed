"""
Custom Logging Handlers for the logs app.
Defines a handler that writes log records directly into the Django database.
"""

import logging
from .utils import redact_phi, redact_message

_internal_logger = logging.getLogger('app')


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
            user_id = getattr(record, 'user_id', None)
            project_id = getattr(record, 'project_id', None)
            object_id = getattr(record, 'object_id', None)

            from django.contrib.auth.models import User
            from django.apps import apps
            from .context import _thread_locals

            # Use dynamic model loading to avoid circular imports during
            # startup
            try:
                log_entry_model = apps.get_model('logs', 'LogEntry')
                project_model = apps.get_model('project', 'Project')
            except (LookupError, RuntimeError):
                # Models might not be ready during early initialization
                return

            # Fetch the actual objects from the database using the IDs
            user = None
            if user_id:
                try:
                    user = User.objects.get(id=user_id)
                except User.DoesNotExist:
                    pass

            project = None
            if project_id:
                try:
                    project = project_model.objects.get(identifier=project_id)
                except project_model.DoesNotExist:
                    pass

            # Extract request metadata from thread-local storage
            request = getattr(_thread_locals, 'request', None)
            ip_address = None
            user_agent = None
            path = None

            if request:
                # IP Address handling (accounting for proxies)
                x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
                if x_forwarded_for:
                    ip_address = x_forwarded_for.split(',')[0].strip()
                else:
                    ip_address = request.META.get('REMOTE_ADDR')
                
                user_agent = request.META.get('HTTP_USER_AGENT')
                path = request.path

            # Apply PHI/PII redaction to message and context data
            safe_message = redact_message(record.getMessage())
            context_data = getattr(record, 'context_data', {})
            safe_context_data = redact_phi(context_data)

            # Create the database record
            log_entry_model.objects.create(
                user=user,
                project=project,
                category=getattr(record, 'category', 'project'),
                level=record.levelname,
                message=safe_message,
                context_data=safe_context_data,
                ip_address=ip_address,
                user_agent=user_agent,
                path=path,
                object_id=object_id
            )

        except Exception as e:
            # CRITICAL: A failure in logging should NEVER crash the main application.
            # We catch all exceptions and fail gracefully, logging the error
            # to the internal console logger.
            _internal_logger.debug(f"Database logging failed: {e}")
