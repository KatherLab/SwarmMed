import logging
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import AbstractBaseModel

# We use a standard logger for the signal itself
_logger = logging.getLogger(__name__)


@receiver(post_save)
def audit_log_save(sender, instance, created, **kwargs):
    """
    Automatically creates an audit log entry when any model inheriting
    from AbstractBaseModel is saved.
    """
    # 1. Skip models that don't inherit from AbstractBaseModel
    if not isinstance(instance, AbstractBaseModel):
        return

    # 2. Prevent infinite loops by skipping log-related models
    # (Although they don't inherit from AbstractBaseModel yet, this is safe)
    if sender.__name__ in ["LogEntry", "LogSigningKey"]:
        return

    try:
        from logs.logger import get_logger
        from logs.context import RequestContextMiddleware

        # Get the current request context (IP, User, etc.) if available
        context = RequestContextMiddleware.get_context()
        user = context.get("user") if context else None

        # If no user in context, check if instance has a user/author field
        if not user:
            for field in ["user", "author", "creator"]:
                if hasattr(instance, field):
                    potential_user = getattr(instance, field)
                    from django.contrib.auth.models import User

                    if isinstance(potential_user, User):
                        user = potential_user
                        break

        log = get_logger()
        action = "CREATED" if created else "UPDATED"
        model_name = sender.__name__
        object_repr = str(instance)

        message = f"{model_name} {action}: {object_repr}"

        # Determine the log category based on the app label
        from logs.models import LogCategory

        app_label = sender._meta.app_label
        category = LogCategory.PROJECT  # Default

        if app_label in LogCategory.values:
            category = app_label

        # Log the event
        log.log(
            category=category,
            level="INFO",
            message=message,
            user=user,
            object_id=str(instance.identifier),
        )

    except Exception as e:
        # We don't want audit logging failures to crash the main application
        _logger.warning(f"Auto audit log failed for {sender.__name__}: {e}")
