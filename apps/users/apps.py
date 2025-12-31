"""
Configuration for the Users application.
This module defines the UsersConfig class which Django uses to manage the app's lifecycle.
"""

from django.apps import AppConfig


class UsersConfig(AppConfig):
    """
    Standard Django AppConfig for the 'users' application.
    """
    # Use 64-bit integers for primary keys by default.
    default_auto_field = "django.db.models.BigAutoField"

    # Python path to the application.
    name = "apps.users"

    def ready(self):
        """
        Executed when the application is started.
        Used to register signal handlers defined in signals.py.
        """
        # Importing signals here ensures they are connected to the models.
        import apps.users.signals  # noqa
