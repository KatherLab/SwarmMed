"""
Custom Logging Handlers for the logs app.
Defines a handler that writes log records directly into the Django database.
"""

import logging

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
            # We only perform database logging if we have a user and project context.
            # These are usually injected into the 'record' by the main Logger
            # class.
            user_id = getattr(record, 'user_id', None)
            project_id = getattr(record, 'project_id', None)

            if not (user_id and project_id):
                # Skip database logging if context is missing
                return

            from django.contrib.auth.models import User
            from django.apps import apps

            # Use dynamic model loading to avoid circular imports during
            # startup
            try:
                log_entry_model = apps.get_model('logs', 'LogEntry')
                project_model = apps.get_model('project', 'Project')
            except (LookupError, RuntimeError):
                # Models might not be ready during early initialization
                return

            # Fetch the actual objects from the database using the IDs
            user = User.objects.get(id=user_id)
            project = project_model.objects.get(identifier=project_id)

            # Create the database record
            log_entry_model.objects.create(
                user=user,
                project=project,
                category=getattr(record, 'category', 'project'),
                level=record.levelname,
                message=record.getMessage(),
                context_data=getattr(record, 'context_data', {})
            )

        except Exception as e:
            # CRITICAL: A failure in logging should NEVER crash the main application.
            # We catch all exceptions and fail gracefully, logging the error
            # to the internal console logger.
            _internal_logger.debug(f"Database logging failed: {e}")
