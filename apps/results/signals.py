"""Signal handlers for the results application."""

from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import QuerySet
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from logs import logger

from .models import (
    ExplanationCase,
    ExplanationPanel,
    ExplanationRun,
    ExplanationSummary,
)


def explanation_storage_prefix(run: ExplanationRun) -> str:
    """Return the storage folder that holds a run's explanation images."""
    return f"{run.project.identifier}/plots/explanations/{run.identifier}/"


@receiver(pre_delete, sender=ExplanationRun)
def delete_explanation_images(sender, instance, **kwargs):
    """Delete a run's stored images once its deletion is committed.

    Django does not delete stored files with their rows, and deleting a
    training job cascades to its explanation runs.
    """
    from project.tasks import cleanup_project_files

    prefix = explanation_storage_prefix(instance)
    transaction.on_commit(lambda: cleanup_project_files.delay(prefix))


# Deleting one of these does not delete its run, so the run's handler above
# does not remove the images.
_PARTS_OF_A_RUN = (ExplanationCase, ExplanationPanel, ExplanationSummary)


def _deleted_without_its_run(origin) -> bool:
    model = origin.model if isinstance(origin, QuerySet) else type(origin)
    return issubclass(model, _PARTS_OF_A_RUN)


@receiver(post_delete, sender=ExplanationPanel)
@receiver(post_delete, sender=ExplanationSummary)
def delete_explanation_image(sender, instance, origin=None, **kwargs):
    """Delete the image of a panel or summary deleted without its run.

    This covers deleting a case (which deletes its panels) or a single image
    record, for example in a Django shell; the web interface and the CLI only
    delete whole runs.
    """
    name = instance.image.name
    if not name or (origin is not None and not _deleted_without_its_run(origin)):
        return

    def delete_file():
        try:
            default_storage.delete(name)
        except Exception as exc:
            logger.get_logger().results.warning(
                f"Could not delete explanation image {name}: {exc}"
            )

    transaction.on_commit(delete_file)
