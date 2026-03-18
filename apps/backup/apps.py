"""App configuration for the backup app.

Defines the metadata and initialization settings for the backup application.
"""

from django.apps import AppConfig


class BackupConfig(AppConfig):
    """Configuration class for the backup application."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "backup"
    verbose_name = "System Backups"
