"""
Configuration for the Home application.
This module defines the HomeConfig class which Django uses to manage the app's lifecycle.
"""

from django.apps import AppConfig


class HomeConfig(AppConfig):
    """
    Standard Django AppConfig for the 'home' application.
    """
    # Specifies the type of auto-generated primary key for models in this app.
    default_auto_field = "django.db.models.BigAutoField"

    # The full Python path to the application.
    name = "home"
