"""
Admin configuration for the training application.
Registers training jobs with the Django admin interface.
"""

from django.contrib import admin
from django.utils.translation import ngettext
from django.contrib import messages
from unfold.admin import ModelAdmin

from .models import TrainingJob


@admin.register(TrainingJob)
class TrainingJobAdmin(ModelAdmin):
    """Configuration for monitoring training jobs in the admin panel."""

    list_display = (
        "identifier",
        "project",
        "network",
        "status",
        "progress_percent",
        "created_at",
        "completed_at",
    )
    list_filter = ("status", "created_at", "project")
    search_fields = ("identifier", "flare_job_id", "project__title")
    readonly_fields = (
        "identifier",
        "created_at",
        "completed_at",
        "total_rounds",
        "rounds_finished",
        "progress_percent",
        "progress_updated_at",
    )
    date_hierarchy = "created_at"

    fieldsets = (
        (
            "Identification",
            {"fields": ("identifier", "project", "network", "flare_job_id")},
        ),
        ("Status & Timing", {"fields": ("status", "created_at", "completed_at")}),
        (
            "Progress Details",
            {
                "fields": (
                    "total_rounds",
                    "rounds_finished",
                    "progress_percent",
                    "progress_updated_at",
                )
            },
        ),
    )

    actions = ["stop_selected_jobs"]

    def stop_selected_jobs(self, request, queryset):
        """
        Action to manually stop selected training jobs.
        Updates status to STOPPED for jobs that are active.
        """
        # Filter for jobs that can actually be stopped
        stoppable_jobs = queryset.filter(status__in=["STARTING", "RUNNING", "PENDING"])
        updated_count = stoppable_jobs.update(status="STOPPED")

        if updated_count:
            self.message_user(
                request,
                ngettext(
                    "%d job was successfully stopped.",
                    "%d jobs were successfully stopped.",
                    updated_count,
                )
                % updated_count,
                messages.SUCCESS,
            )
        else:
            self.message_user(
                request, "No active jobs selected for stopping.", messages.WARNING
            )

    stop_selected_jobs.short_description = "Stop selected training jobs"
