"""
Admin configuration for the project app.
This module registers the project-related models with the Django admin interface,
allowing administrators to manage projects and user-project relations.
"""

from django.contrib import admin
from .models import Project, UserCurrentProject


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    """
    Configuration for the Project model in the admin panel.
    Provides filtering and search capabilities for easier management.
    """
    # Fields to display in the list view of the admin panel
    list_display = ('title', 'author', 'status', 'creation_date', 'identifier')

    # Enable filtering by status and creation date
    list_filter = ('status', 'creation_date')

    # Allow searching by title, author's username, and unique identifier
    search_fields = ('title', 'author__username', 'identifier')

    # Define which fields are read-only to prevent accidental modification of
    # metadata
    readonly_fields = ('identifier', 'creation_date')


@admin.register(UserCurrentProject)
class UserCurrentProjectAdmin(admin.ModelAdmin):
    """
    Configuration for the UserCurrentProject model in the admin panel.
    Helps track which project each user is currently working on.
    """
    # Display the user and their associated current project
    list_display = ('user', 'project')

    # Enable search by username and project title
    search_fields = ('user__username', 'project__title')
