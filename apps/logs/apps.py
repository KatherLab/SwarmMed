"""
Configuration for the Logs app.
Defines the application configuration class used by Django.
"""

from django.apps import AppConfig


class LogsConfig(AppConfig):
    """
    Standard Django configuration for the logs application.
    """
    # Use 64-bit integers for primary keys by default
    default_auto_field = 'django.db.models.BigAutoField'

    # The full Python path to the application
    name = 'apps.logs'
