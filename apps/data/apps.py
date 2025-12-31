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
    default_auto_field = 'django.db.models.BigAutoField'

    # The full Python path to the application
    name = 'apps.data'

    def ready(self):
        """
        This method is called when the application is loaded.
        We use it to ensure the necessary storage infrastructure is ready.
        """
        # We import here to avoid circular dependencies during startup
        from .utils import create_minio_bucket

        # Ensure the default bucket 'swarmcloud' exists in S3/Minio
        # This is where all project data and scripts will be stored.
        try:
            create_minio_bucket('swarmcloud')
        except Exception:
            # We fail silently or log during startup if the service is not yet up,
            # as it might be starting up in a docker-compose environment.
            pass
