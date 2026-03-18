"""App configuration for the common application.

Defines the configuration class for the common application and handles
any initialization tasks.
"""

from django.apps import AppConfig


class CommonConfig(AppConfig):
    """Configuration for the common Django application.

    Attributes:
        default_auto_field (str): The default auto-incrementing primary key field type.
        name (str): The name of the Django application.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "common"

    def ready(self):
        """Import signal handlers when the application is ready."""
        import common.signals  # noqa
