"""
Admin configuration for the data app.
This file registers the models with the Django admin interface,
allowing administrators to manage Validation and Visualization runs.
"""

from django.contrib import admin
from .models import ValidationRun, ValidationCheck, VisualizationRun, VisualizationPlot


class ValidationCheckInline(admin.TabularInline):
    model = ValidationCheck
    extra = 0
    fields = ("name", "status", "message")
    readonly_fields = ("name", "status", "message")
    can_delete = False


@admin.register(ValidationRun)
class ValidationRunAdmin(admin.ModelAdmin):
    """
    Admin interface for ValidationRun model.
    Displays key fields in the list view for easier monitoring.
    """

    list_display = ("id", "project", "user", "status", "success", "created_at")
    list_filter = ("status", "success", "created_at")
    search_fields = ("project__title", "user__username", "id")
    readonly_fields = (
        "id",
        "created_at",
        "started_at",
        "completed_at",
        "output",
        "error_message",
        "celery_task_id",
    )

    inlines = [ValidationCheckInline]

    fieldsets = (
        ("Run Information", {"fields": ("project", "user", "id", "celery_task_id")}),
        (
            "Status & Outcome",
            {"fields": ("status", "success", "output", "error_message")},
        ),
        (
            "System Metadata",
            {
                "fields": ("created_at", "started_at", "completed_at"),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(ValidationCheck)
class ValidationCheckAdmin(admin.ModelAdmin):
    """
    Admin interface for ValidationCheck model.
    Helps admins inspect individual checks within a validation run.
    """

    list_display = ("name", "validation_run", "status")
    list_filter = ("status",)
    search_fields = ("name", "message")


class VisualizationPlotInline(admin.TabularInline):
    model = VisualizationPlot
    extra = 0
    fields = ("title", "plot_number", "image_data")
    readonly_fields = ("title", "plot_number", "image_data")
    can_delete = False


@admin.register(VisualizationRun)
class VisualizationRunAdmin(admin.ModelAdmin):
    """
    Admin interface for VisualizationRun model.
    """

    list_display = ("id", "project", "user", "status", "success", "created_at")
    list_filter = ("status", "success", "created_at")
    search_fields = ("project__title", "user__username", "id")
    readonly_fields = (
        "id",
        "created_at",
        "started_at",
        "completed_at",
        "output",
        "error_message",
        "celery_task_id",
    )

    inlines = [VisualizationPlotInline]

    fieldsets = (
        ("Run Information", {"fields": ("project", "user", "id", "celery_task_id")}),
        (
            "Status & Outcome",
            {"fields": ("status", "success", "output", "error_message")},
        ),
        (
            "System Metadata",
            {
                "fields": ("created_at", "started_at", "completed_at"),
                "classes": ("collapse",),
            },
        ),
    )


@admin.register(VisualizationPlot)
class VisualizationPlotAdmin(admin.ModelAdmin):
    """
    Admin interface for VisualizationPlot model.
    """

    list_display = ("title", "visualization_run", "plot_number", "created_at")
    list_filter = ("created_at",)
    search_fields = ("title",)
