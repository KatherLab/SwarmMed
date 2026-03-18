"""Configuration for the Network app.

Defines the application configuration class used by Django.
"""

from django.apps import AppConfig


class NetworkConfig(AppConfig):
    """Standard Django configuration for the network application."""

    # Specifies the type of auto-generated primary key for models in this app
    default_auto_field = "django.db.models.BigAutoField"

    # The full Python path to the application
    name = "network"
