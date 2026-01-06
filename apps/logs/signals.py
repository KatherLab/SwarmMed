"""
Signal handlers for the logs application.
Listen for data-modifying events in other applications to create audit entries.
"""

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from .logger import get_logger

# We import models dynamically within handlers to avoid circular imports


@receiver(post_save)
def log_model_save(sender, instance, created, **kwargs):
    """
    Generic signal handler to log the creation or update of important models.
    """
    app_label = sender._meta.app_label
    model_name = sender._meta.model_name

    # List of models we specifically want to audit on save
    AUDITED_MODELS = {
        "project": ["project"],
        "data": ["validationrun", "visualizationrun"],
        "network": ["swarmnetwork"],
        "training": ["trainingjob"],
        "results": ["trainingresult"],
    }

    if app_label in AUDITED_MODELS and model_name in AUDITED_MODELS[app_label]:
        logger = get_logger()
        action = "Created" if created else "Updated"
        category = app_label

        # Handle some category mapping mismatches if necessary
        if category == "project":
            category = "project"

        message = (
            f"Audit: {action} {model_name} '{instance}' (ID: {instance.pk})"
        )

        # Determine log level
        level = "INFO"

        # Capture some basic context
        context = {
            "model": model_name,
            "app": app_label,
            "object_id": str(instance.pk),
            "action": action.lower(),
        }

        # If it's a project, try to link it
        project_obj = None
        if hasattr(instance, "project"):
            project_obj = instance.project
        elif model_name == "project":
            project_obj = instance

        logger.log(
            level=level,
            message=message,
            category=category,
            project=project_obj,
            **context,
        )


@receiver(post_delete)
def log_model_delete(sender, instance, **kwargs):
    """
    Generic signal handler to log the deletion of important models.
    """
    app_label = sender._meta.app_label
    model_name = sender._meta.model_name

    AUDITED_MODELS = {
        "project": ["project"],
        "network": ["swarmnetwork"],
    }

    if app_label in AUDITED_MODELS and model_name in AUDITED_MODELS[app_label]:
        logger = get_logger()
        message = (
            f"Audit: Deleted {model_name} '{instance}' (ID: {instance.pk})"
        )

        context = {
            "model": model_name,
            "app": app_label,
            "object_id": str(instance.pk),
            "action": "delete",
        }

        logger.log(
            level="WARNING", message=message, category=app_label, **context
        )


@receiver(m2m_changed)
def log_m2m_changes(sender, instance, action, pk_set, **kwargs):
    """
    Logs changes to Many-to-Many relationships, like Project members.
    """
    model_name = instance._meta.model_name

    if model_name == "project" and "members" in str(sender):
        if action in ["post_add", "post_remove", "post_clear"]:
            logger = get_logger()
            message = f"Audit: Project members changed for '{instance}' - Action: {action}"

            logger.project.info(
                message,
                object_id=str(instance.pk),
                action=action,
                member_pks=list(pk_set) if pk_set else [],
            )
