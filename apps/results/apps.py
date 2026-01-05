"""
Configuration for the Results application.
This module defines the ResultsConfig class which Django uses to manage the app's lifecycle.
"""

from django.apps import AppConfig


class ResultsConfig(AppConfig):
    """
    Standard Django AppConfig for the 'results' application.
    """

    # Specifies the type of auto-generated primary key for models in this app.
    # BigAutoField is a 64-bit integer, recommended for large databases.
    default_auto_field = "django.db.models.BigAutoField"

    # The full Python path to the application.
    name = "results"
