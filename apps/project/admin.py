"""
Admin configuration for the project app.
This module registers the project-related models with the Django admin interface,
allowing administrators to manage projects and user-project relations.
"""

from django.contrib import admin
from .models import Project, UserCurrentProject
from network.models import SwarmNetwork
from training.models import TrainingJob
from data.models import ValidationRun, VisualizationRun
from logs.models import LogEntry


class SwarmNetworkInline(admin.TabularInline):
    model = SwarmNetwork
    extra = 0
    fields = ("name", "status", "created_at")
    readonly_fields = ("created_at",)
    can_delete = False


class TrainingJobInline(admin.TabularInline):
    model = TrainingJob
    extra = 0
    fields = ("identifier", "status", "created_at")
    readonly_fields = ("identifier", "created_at")
    can_delete = False


class ValidationRunInline(admin.TabularInline):
    model = ValidationRun
    extra = 0
    fields = ("id", "status", "success", "created_at")
    readonly_fields = ("id", "created_at")
    can_delete = False


class VisualizationRunInline(admin.TabularInline):
    model = VisualizationRun
    extra = 0
    fields = ("id", "status", "success", "created_at")
    readonly_fields = ("id", "created_at")
    can_delete = False


class LogEntryInline(admin.TabularInline):
    model = LogEntry
    extra = 0
    fields = ("timestamp", "level", "category", "user", "message")
    readonly_fields = ("timestamp", "level", "category", "user", "message")
    can_delete = False
    max_num = 10  # Show only the most recent logs


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    """
    Configuration for the Project model in the admin panel.
    Provides filtering and search capabilities for easier management.
    """

    # Fields to display in the list view of the admin panel
    list_display = (
        "title",
        "author",
        "status",
        "created_at",
        "get_members_count",
        "identifier",
    )

    # Enable filtering by status and creation date
    list_filter = ("status", "created_at")

    # Allow searching by title, author's username, and unique identifier
    search_fields = ("title", "author__username", "identifier")

    # Define which fields are read-only to prevent accidental modification of
    # metadata
    readonly_fields = ("identifier", "created_at")

    date_hierarchy = "created_at"

    # Add inlines for related models to give a full overview
    inlines = [
        SwarmNetworkInline,
        TrainingJobInline,
        ValidationRunInline,
        VisualizationRunInline,
        LogEntryInline,
    ]

    actions = ["archive_projects"]

    fieldsets = (
        (
            "Project Details",
            {"fields": ("title", "description", "author", "status", "members")},
        ),
        ("System Metadata", {"fields": ("identifier", "created_at")}),
        (
            "Source Files",
            {
                "fields": (
                    "training_code",
                    "requirements_file",
                    "data_validation_script",
                    "data_visualization_script",
                    "results_visualization_script",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    def get_members_count(self, obj):
        return obj.members.count()

    get_members_count.short_description = "Members"

    def archive_projects(self, request, queryset):
        rows_updated = queryset.update(status="ARCHIVED")
        if rows_updated == 1:
            message_bit = "1 project was"
        else:
            message_bit = f"{rows_updated} projects were"
        self.message_user(request, f"{message_bit} successfully archived.")

    archive_projects.short_description = "Archive selected projects"


@admin.register(UserCurrentProject)
class UserCurrentProjectAdmin(admin.ModelAdmin):
    """
    Configuration for the UserCurrentProject model in the admin panel.
    Helps track which project each user is currently working on.
    """

    # Display the user and their associated current project
    list_display = ("user", "project")

    # Enable search by username and project title
    search_fields = ("user__username", "project__title")
