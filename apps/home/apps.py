"""App configuration for the home app.

Defines the metadata and initialization settings for the home application.
"""

from django.apps import AppConfig


class HomeConfig(AppConfig):
    """Configuration class for the home application.

    Attributes:
        default_auto_field (str): The name of the field to use for primary keys
            when none is specified.
        name (str): The full Python path to the application.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "home"
