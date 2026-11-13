"""Admin configuration for the results application.

Registers the models with the Django admin interface to allow
administrators to manage training results and visualization runs.
"""

from django.contrib import admin
from unfold.admin import ModelAdmin

from common.admin_filters import ProjectFilter_ByJob, ProjectFilter_Generic

from .models import (
    ExplanationCase,
    ExplanationReview,
    ExplanationRun,
    ResultsVisualizationPlot,
    ResultsVisualizationRun,
    TrainingResult,
)


@admin.register(TrainingResult)
class TrainingResultAdmin(ModelAdmin):
    """Configuration for managing individual training result files in admin."""

    list_display = ("identifier", "job", "file_size", "created_at")
    # Allow filtering by the project that the associated job belongs to
    list_filter = ("created_at", ProjectFilter_ByJob)
    search_fields = ("identifier", "file_path", "job__identifier")
    readonly_fields = ("identifier", "created_at")


@admin.register(ResultsVisualizationRun)
class ResultsVisualizationRunAdmin(ModelAdmin):
    """Configuration for monitoring visualization script executions."""

    list_display = ("id", "project", "user", "status", "success", "created_at")
    list_filter = ("status", "success", "created_at")
    search_fields = ("id", "project__title", "user__username")
    readonly_fields = ("id", "created_at", "started_at", "completed_at")

    fieldsets = (
        ("Run Information", {"fields": ("project", "user", "id")}),
        ("Status & Outcome", {"fields": ("status", "success")}),
        (
            "System Metadata",
            {
                "fields": ("created_at", "started_at", "completed_at"),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(ResultsVisualizationPlot)
class ResultsVisualizationPlotAdmin(ModelAdmin):
    """Configuration for inspecting generated plots."""

    list_display = ("title", "visualization_run", "plot_number", "created_at")
    list_filter = ("created_at",)
    readonly_fields = ("created_at",)


class _ReadOnlyAdmin(ModelAdmin):
    """Admin that shows records without allowing them to be added or edited."""

    def has_add_permission(self, request):
        """Records are created by the application only."""
        return False

    def has_change_permission(self, request, obj=None):
        """Records are never edited in the admin."""
        return False


class _NoDeleteAdmin(_ReadOnlyAdmin):
    """Read-only admin that does not delete records either.

    Runs are deleted in the web interface, where only the people allowed to do so
    can, or with their project; cases and reviews are deleted only with their run
    (which also deletes the run's images). Reviews are append-only.
    """

    def has_delete_permission(self, request, obj=None):
        """Records are never deleted in the admin."""
        return False


@admin.register(ExplanationRun)
class ExplanationRunAdmin(_NoDeleteAdmin):
    """Explanation runs (sandbox runs and imported bundles)."""

    list_display = (
        "id",
        "project",
        "user",
        "source",
        "status",
        "success",
        "created_at",
    )
    list_filter = ("status", "source", "created_at", ProjectFilter_Generic)
    search_fields = ("identifier", "project__title", "user__username")
    readonly_fields = (
        "identifier",
        "project",
        "job",
        "flare_job_id",
        "user",
        "source",
        "status",
        "success",
        "error_message",
        "info",
        "script_sha256",
        "created_at",
        "started_at",
        "completed_at",
        "output",
    )


@admin.register(ExplanationCase)
class ExplanationCaseAdmin(_NoDeleteAdmin):
    """Explained cases."""

    list_display = ("identifier", "run", "position", "outcome", "created_at")
    list_filter = ("outcome", "created_at")
    readonly_fields = (
        "identifier",
        "run",
        "position",
        "case_id",
        "label",
        "prediction",
        "target",
        "score",
        "outcome",
        "details",
        "created_at",
    )


@admin.register(ExplanationReview)
class ExplanationReviewAdmin(_NoDeleteAdmin):
    """Reviews are append-only: they can be viewed, not changed."""

    list_display = (
        "identifier",
        "case",
        "reviewer",
        "verdict",
        "blinded",
        "created_at",
    )
    list_filter = ("verdict", "blinded", "created_at")
    readonly_fields = (
        "identifier",
        "case",
        "reviewer",
        "verdict",
        "note",
        "blinded",
        "created_at",
    )
