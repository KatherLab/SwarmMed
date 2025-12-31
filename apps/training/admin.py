"""
Admin configuration for the training application.
Registers training jobs with the Django admin interface.
"""

from django.contrib import admin

from .models import TrainingJob


@admin.register(TrainingJob)
class TrainingJobAdmin(admin.ModelAdmin):
    """Configuration for monitoring training jobs in the admin panel."""
    list_display = (
        'identifier',
        'project',
        'network',
        'status',
        'created_at',
        'completed_at'
    )
    list_filter = ('status', 'created_at', 'project')
    search_fields = ('identifier', 'flare_job_id', 'project__title')
    readonly_fields = ('identifier', 'created_at')
