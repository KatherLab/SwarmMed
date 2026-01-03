"""
Admin configuration for the communication app.
This file registers the models with the Django admin interface,
allowing administrators to manage Messages and ProjectPosts.
"""

from django.contrib import admin
from unfold.admin import ModelAdmin
from .models import Message, ProjectPost, ProjectBoardAccess


@admin.register(Message)
class MessageAdmin(ModelAdmin):
    """
    Configuration for the Message model in the admin panel.
    """

    list_display = ("sender", "recipient", "created_at", "is_read", "subject_preview")
    list_filter = ("is_read", "created_at")
    search_fields = ("sender__username", "recipient__username")
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"

    def subject_preview(self, obj):
        return (
            obj.subject[:50] + "..."
            if obj.subject and len(obj.subject) > 50
            else obj.subject
        )

    subject_preview.short_description = "Subject"


@admin.register(ProjectPost)
class ProjectPostAdmin(ModelAdmin):
    """
    Configuration for the ProjectPost model in the admin panel.
    """

    list_display = ("project", "author", "created_at")
    list_filter = ("created_at", "project")
    search_fields = ("project__title", "author__username")
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"


@admin.register(ProjectBoardAccess)
class ProjectBoardAccessAdmin(ModelAdmin):
    """
    Configuration for the ProjectBoardAccess model.
    """

    list_display = ("user", "project", "updated_at")
    search_fields = ("user__username", "project__title")
    readonly_fields = ("updated_at",)
