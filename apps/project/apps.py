"""
Configuration for the Project application.
This module defines the ProjectConfig class which Django uses to manage the app's lifecycle.
"""

from django.apps import AppConfig


class ProjectConfig(AppConfig):
    """
    Standard Django AppConfig for the 'project' application.
    """
    # Specifies the type of auto-generated primary key for models in this app.
    # BigAutoField is a 64-bit integer, recommended for large databases.
    default_auto_field = 'django.db.models.BigAutoField'

    # The full Python path to the application.
    name = 'apps.project'

    def ready(self):
        """
        This method is called as soon as the application is loaded by Django.
        We use it to import signal handlers to ensure they are registered
        when the server starts.
        """
        # Import signals to make sure @receiver decorators are executed.
        import apps.project.signals  # noqa: F401
