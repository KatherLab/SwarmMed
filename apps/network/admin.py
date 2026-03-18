"""Admin configuration for the network app.

This file registers the models with the Django admin interface,
allowing administrators to manage Swarm Networks and Participants.
"""

from django.contrib import admin, messages
from django.utils.translation import ngettext
from unfold.admin import ModelAdmin, TabularInline

from common.admin_filters import ProjectFilter_ByNetwork
from training.models import TrainingJob

from .models import SwarmNetwork, SwarmParticipant, UserCurrentNetwork
from .tasks import stop_swarm_network_task


class SwarmParticipantInline(TabularInline):
    """Inline admin for SwarmParticipant."""

    model = SwarmParticipant
    extra = 0
    fields = ("user", "role", "participant_id")
    can_delete = True


class TrainingJobInline(TabularInline):
    """Inline admin for TrainingJob."""

    model = TrainingJob
    extra = 0
    fields = ("identifier", "status", "created_at")
    readonly_fields = ("identifier", "created_at")
    can_delete = False


@admin.register(SwarmNetwork)
class SwarmNetworkAdmin(ModelAdmin):
    """Admin interface for SwarmNetwork model."""

    list_display = ("name", "project", "status", "author", "created_at")
    list_filter = ("status", "project")
    search_fields = ("name", "description")
    readonly_fields = ("identifier", "created_at", "updated_at")
    date_hierarchy = "created_at"

    inlines = [
        SwarmParticipantInline,
        TrainingJobInline,
    ]

    fieldsets = (
        (
            "Network Information",
            {"fields": ("name", "description", "project", "author")},
        ),
        ("Status & Lifecycle", {"fields": ("status",)}),
        (
            "System Metadata",
            {
                "fields": ("identifier", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    actions = ["stop_selected_networks"]

    def stop_selected_networks(self, request, queryset):
        """Action to stop selected swarm networks using the celery task.

        Args:
            request (HttpRequest): The current HTTP request.
            queryset (QuerySet): The selected SwarmNetwork instances.
        """
        count = 0
        for network in queryset:
            if network.status in ["RUNNING", "STARTING", "INITIALIZING"]:
                # Trigger the celery task
                # Note: This requires the network author's ID, but we might be admin.
                # Ideally, we should use the admin's ID or the network author's.
                # For safety, we'll use the network author if available, else current user.
                user_id = (
                    network.author.id if network.author else request.user.id
                )
                stop_swarm_network_task.delay(str(network.identifier), user_id)
                count += 1

        if count:
            self.message_user(
                request,
                ngettext(
                    "%d network stop task was initiated.",
                    "%d network stop tasks were initiated.",
                    count,
                )
                % count,
                messages.SUCCESS,
            )
        else:
            self.message_user(
                request, "No active networks were selected.", messages.WARNING
            )

    stop_selected_networks.short_description = "Stop selected swarm networks"


@admin.register(SwarmParticipant)
class SwarmParticipantAdmin(ModelAdmin):
    """Admin interface for SwarmParticipant model."""

    list_display = ("user", "network", "role", "participant_id")
    # Allow filtering participants by their role, network, and the project
    # that the network belongs to.
    list_filter = ("role", "network", ProjectFilter_ByNetwork)
    search_fields = ("participant_id", "user__username")


@admin.register(UserCurrentNetwork)
class UserCurrentNetworkAdmin(ModelAdmin):
    """Admin interface for tracking users' current active network."""

    list_display = ("user", "network")
    search_fields = ("user__username", "network__name")
