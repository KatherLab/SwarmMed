"""Admin configuration for the communication app.

This file registers the models with the Django admin interface,
allowing administrators to manage Messages and ProjectPosts.
"""

from django.contrib import admin
from unfold.admin import ModelAdmin

from common.admin_filters import ProjectFilter_Generic

from .models import Message, ProjectBoardAccess, ProjectPost


@admin.register(Message)
class MessageAdmin(ModelAdmin):
    """Configuration for the Message model in the admin panel.

    Attributes:
        list_display (tuple): Fields to display in the admin list view.
        list_filter (tuple): Fields to use for filtering in the admin list view.
        search_fields (tuple): Fields to search when searching in the admin panel.
        readonly_fields (tuple): Fields that are read-only in the admin panel.
        date_hierarchy (str): The date field to use for the admin date hierarchy.
    """

    list_display = (
        "sender",
        "recipient",
        "created_at",
        "is_read",
        "subject_preview",
    )
    list_filter = ("is_read", "created_at")
    search_fields = ("sender__username", "recipient__username")
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"

    def subject_preview(self, obj):
        """Returns a preview of the message subject.

        Args:
            obj (Message): The message object.

        Returns:
            str: A truncated subject string if it's longer than 50 characters.
        """
        return (
            obj.subject[:50] + "..."
            if obj.subject and len(obj.subject) > 50
            else obj.subject
        )

    subject_preview.short_description = "Subject"


@admin.register(ProjectPost)
class ProjectPostAdmin(ModelAdmin):
    """Configuration for the ProjectPost model in the admin panel.

    Attributes:
        list_display (tuple): Fields to display in the admin list view.
        list_filter (tuple): Fields to use for filtering in the admin list view.
        search_fields (tuple): Fields to search when searching in the admin panel.
        readonly_fields (tuple): Fields that are read-only in the admin panel.
        date_hierarchy (str): The date field to use for the admin date hierarchy.
    """

    list_display = ("project", "author", "created_at")
    # Show project filter as dropdown for ProjectPost
    list_filter = ("created_at", ProjectFilter_Generic)
    search_fields = ("project__title", "author__username")
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"


@admin.register(ProjectBoardAccess)
class ProjectBoardAccessAdmin(ModelAdmin):
    """Configuration for the ProjectBoardAccess model.

    Attributes:
        list_display (tuple): Fields to display in the admin list view.
        search_fields (tuple): Fields to search when searching in the admin panel.
        readonly_fields (tuple): Fields that are read-only in the admin panel.
    """

    list_display = ("user", "project", "updated_at")
    search_fields = ("user__username", "project__title")
    readonly_fields = ("updated_at",)
