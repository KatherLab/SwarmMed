"""
Admin configuration for the results application.
Registers the models with the Django admin interface to allow
administrators to manage training results and visualization runs.
"""

from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import (
    ResultsVisualizationPlot,
    ResultsVisualizationRun,
    TrainingResult,
)


@admin.register(TrainingResult)
class TrainingResultAdmin(ModelAdmin):
    """Configuration for managing individual training result files in admin."""

    list_display = ("identifier", "job", "file_size", "created_at")
    list_filter = ("created_at",)
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
