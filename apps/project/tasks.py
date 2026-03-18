"""Celery tasks for the project application.
Handles background tasks like asynchronous file deletion to improve web response times.
"""

import logging
import os
import shutil

from django.conf import settings
from django.core.files.storage import default_storage

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def cleanup_project_files(folder_path):
    """Deletes a folder and its contents from the storage backend (S3 or local).
    This is intended to be run in the background.
    """
    try:
        if hasattr(default_storage, "bucket"):
            # Using Amazon S3 storage
            prefix = folder_path
            s3_objects = default_storage.bucket.objects.filter(Prefix=prefix)
            s3_objects.delete()
            logger.info(f"Successfully deleted S3 folder: {prefix}")
        else:
            # Using standard local filesystem storage
            full_path = os.path.join(settings.MEDIA_ROOT, folder_path)
            if os.path.exists(full_path):
                shutil.rmtree(full_path)
                # Re-create the folder so it's ready for new files if needed
                os.makedirs(full_path, exist_ok=True)
                logger.info(f"Successfully deleted local folder: {full_path}")
    except Exception as e:
        logger.error(f"Error during file cleanup for {folder_path}: {str(e)}")


@shared_task
def delete_all_project_files(project_identifier):
    """Deletes all files associated with a project identifier when the project is deleted.
    Includes both the uploaded media files and the local workspace directory.
    """
    try:
        # 1. Delete media files (S3 or local)
        folder_path = f"{str(project_identifier)}/"
        if hasattr(default_storage, "bucket"):
            prefix = folder_path
            s3_objects = default_storage.bucket.objects.filter(Prefix=prefix)
            s3_objects.delete()
            logger.info(
                f"Successfully deleted all S3 files for project: {project_identifier}"
            )
        else:
            full_path = os.path.join(settings.MEDIA_ROOT, folder_path)
            if os.path.exists(full_path):
                shutil.rmtree(full_path)
                logger.info(
                    f"Successfully deleted all local files for project: {project_identifier}"
                )

        # 2. Delete local workspace directory
        workspace_path = os.path.join(
            settings.BASE_DIR, "workspaces", str(project_identifier)
        )
        if os.path.exists(workspace_path):
            shutil.rmtree(workspace_path)
            logger.info(
                f"Successfully deleted local workspace for project: {project_identifier}"
            )

    except Exception as e:
        logger.error(
            f"Error during full project cleanup for {project_identifier}: {str(e)}"
        )
