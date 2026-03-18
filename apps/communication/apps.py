"""Configuration for the Communication app.

This file defines the application configuration class used by Django.
"""

from django.apps import AppConfig


class CommunicationConfig(AppConfig):
    """Configuration class for the communication application.

    Attributes:
        default_auto_field (str): The default auto field type for models.
        name (str): The full Python path to the application.
    """

    # Use 64-bit integers for primary keys by default
    default_auto_field = "django.db.models.BigAutoField"

    # The full Python path to the application
    name = "communication"

    def ready(self):
        """Initializes the application when it is loaded.

        This method is used to import and connect signal handlers.
        """
        # Import the signals module to ensure they are registered
        import communication.signals  # noqa: F401
