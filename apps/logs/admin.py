"""
Admin configuration for the logs app.
Registers the LogEntry model with the Django admin interface.
"""

from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import LogEntry, LogSigningKey


@admin.register(LogEntry)
class LogEntryAdmin(ModelAdmin):
    """
    Configuration for the LogEntry model in the admin panel.
    Provides filtering and search capabilities for administrators.
    """

    # Fields to display in the list view
    list_display = (
        "timestamp",
        "level",
        "category",
        "user",
        "ip_address",
        "path",
        "message",
    )

    # Enable filtering by these fields
    list_filter = ("level", "category", "timestamp", "source")

    # Allow searching by message, user email, and project title
    search_fields = (
        "message",
        "user__email",
        "user__username",
        "user_identifier",
        "project__title",
        "ip_address",
    )

    # Make all fields read-only to prevent manual modification of logs
    readonly_fields = (
        "id",
        "user",
        "user_identifier",
        "project",
        "category",
        "swarm_network",
        "timestamp",
        "level",
        "message",
        "context_data",
        "ip_address",
        "user_agent",
        "path",
        "object_id",
        "signing_key",
        "previous_hash",
        "signature",
    )

    date_hierarchy = "timestamp"

    def has_add_permission(self, request):
        """Logs should only be created by the system, not manually."""
        return False

    def has_change_permission(self, request, obj=None):
        """Logs should not be modified once created."""
        return False


@admin.register(LogSigningKey)
class LogSigningKeyAdmin(ModelAdmin):
    """
    Configuration for auditing LogSigningKeys.
    Keys are critical for integrity and should be read-only.
    """

    list_display = ("id", "is_active", "created_at")
    list_filter = ("is_active", "created_at")
    readonly_fields = ("id", "key", "created_at")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        """Keys are generated automatically by the system."""
        return False

    def has_change_permission(self, request, obj=None):
        """Keys should not be modified manually."""
        return False
