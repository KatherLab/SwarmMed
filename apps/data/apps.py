"""
Configuration for the Data app.
This file defines the application configuration class used by Django.
"""

from django.apps import AppConfig


class DataConfig(AppConfig):
    """
    Configuration class for the data application.
    Handles app initialization, such as setting up storage buckets.
    """

    # Use 64-bit integers for primary keys by default
    default_auto_field = "django.db.models.BigAutoField"

    # The full Python path to the application
    name = "data"

    def ready(self):
        """
        This method is called when the application is loaded.
        We use it to ensure the necessary storage infrastructure is ready.
        """
        # We import here to avoid circular dependencies during startup
        from .utils import create_minio_bucket
        from logs.logger import get_logger
        from django.conf import settings

        logger = get_logger()

        # Ensure the default bucket exists in S3/Minio
        # This is where all project data and scripts will be stored.
        bucket_name = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "swarmcloud")
        if not bucket_name:
            bucket_name = "swarmcloud"

        try:
            create_minio_bucket(bucket_name)
        except Exception as e:
            # We log during startup if the service is not yet up,
            # as it might be starting up in a docker-compose environment.
            logger.data.warning(
                f"Could not ensure '{bucket_name}' bucket during startup: {e}"
            )
