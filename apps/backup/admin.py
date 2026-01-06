from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action

from .models import BackupConfiguration, BackupLog, BackupStatus
from .tasks import run_backup, run_restore


@admin.register(BackupConfiguration)
class BackupConfigurationAdmin(ModelAdmin):
    list_display = (
        "name",
        "storage_backend",
        "is_active",
        "retention_days",
        "created_at",
    )
    list_filter = ("storage_backend", "is_active")
    search_fields = ("name",)

    actions = ["trigger_backup"]

    @action(description=_("Trigger Manual Backup"), url_path="trigger-backup")
    def trigger_backup(self, request, queryset):
        for config in queryset:
            log = BackupLog.objects.create(config=config)
            run_backup.delay(log.id)

        self.message_user(
            request,
            _("Manual backup(s) triggered in the background."),
            messages.SUCCESS,
        )


@admin.register(BackupLog)
class BackupLogAdmin(ModelAdmin):
    list_display = (
        "started_at",
        "status",
        "filename",
        "file_size_display",
        "config",
    )
    list_filter = ("status", "config", "started_at")
    readonly_fields = (
        "identifier",
        "config",
        "status",
        "filename",
        "file_size",
        "storage_location",
        "started_at",
        "finished_at",
        "error_message",
        "included_databases",
        "has_media",
    )

    actions = ["trigger_restore"]

    def file_size_display(self, obj):
        from common.utils import format_size

        return format_size(obj.file_size) if obj.file_size else "-"

    file_size_display.short_description = _("File Size")

    @action(
        description=_("Restore from this Backup"), url_path="trigger-restore"
    )
    def trigger_restore(self, request, queryset):
        if queryset.count() > 1:
            self.message_user(
                request,
                _("Please select only one backup to restore from."),
                messages.ERROR,
            )
            return

        log = queryset.first()
        if log.status != BackupStatus.SUCCESS:
            self.message_user(
                request,
                _("Only successful backups can be restored."),
                messages.ERROR,
            )
            return

        run_restore.delay(log.id)
        self.message_user(
            request,
            _(
                "Restore operation triggered in the background. System may be temporarily unavailable."
            ),
            messages.WARNING,
        )
