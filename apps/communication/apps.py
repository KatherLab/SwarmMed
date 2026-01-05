"""
Configuration for the Communication app.
This file defines the application configuration class used by Django.
"""

from django.apps import AppConfig


class CommunicationConfig(AppConfig):
    """
    Configuration class for the communication application.
    """

    # Use 64-bit integers for primary keys by default
    default_auto_field = "django.db.models.BigAutoField"

    # The full Python path to the application
    name = "communication"

    def ready(self):
        """
        This method is called when the application is loaded.
        We use it to import and connect any signal handlers.
        """
        # Import the signals module to ensure they are registered
        import communication.signals  # noqa: F401
