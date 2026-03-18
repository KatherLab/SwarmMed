"""Configuration for the Training application.

This module defines the TrainingConfig class which Django uses to manage the app's lifecycle.
"""

from django.apps import AppConfig


class TrainingConfig(AppConfig):
    """Standard Django AppConfig for the 'training' application."""

    # Specifies the type of auto-generated primary key for models in this app.
    default_auto_field = "django.db.models.BigAutoField"

    # The full Python path to the application.
    name = "training"
