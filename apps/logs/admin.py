"""
Admin configuration for the logs app.
Registers the LogEntry model with the Django admin interface.
"""

from django.contrib import admin

from .models import LogEntry


@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    """
    Configuration for the LogEntry model in the admin panel.
    Provides filtering and search capabilities for administrators.
    """
    # Fields to display in the list view
    list_display = (
        'timestamp',
        'level',
        'category',
        'user',
        'project',
        'message')

    # Enable filtering by these fields
    list_filter = ('level', 'category', 'timestamp')

    # Allow searching by message, user email, and project title
    search_fields = ('message', 'user__email', 'project__title')

    # Make all fields read-only to prevent manual modification of logs
    readonly_fields = (
        'id', 'user', 'project', 'category', 'swarm_network',
        'timestamp', 'level', 'source', 'message', 'context_data'
    )

    def has_add_permission(self, request):
        """Logs should only be created by the system, not manually."""
        return False

    def has_change_permission(self, request, obj=None):
        """Logs should not be modified once created."""
        return False
