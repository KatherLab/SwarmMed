from celery import shared_task
from .models import BackupLog, BackupConfiguration
from .utils import perform_backup, perform_restore

@shared_task(name="backup.tasks.run_backup")
def run_backup(log_id):
    try:
        log = BackupLog.objects.get(id=log_id)
        perform_backup(log)
        return f"Backup {log_id} completed successfully."
    except BackupLog.DoesNotExist:
        return f"BackupLog {log_id} not found."
    except Exception as e:
        return f"Backup {log_id} failed: {str(e)}"

@shared_task(name="backup.tasks.run_restore")
def run_restore(log_id):
    try:
        log = BackupLog.objects.get(id=log_id)
        perform_restore(log)
        return f"Restore from {log_id} completed successfully."
    except BackupLog.DoesNotExist:
        return f"BackupLog {log_id} not found."
    except Exception as e:
        return f"Restore from {log_id} failed: {str(e)}"

@shared_task(name="backup.tasks.scheduled_backup")
def scheduled_backup():
    configs = BackupConfiguration.objects.filter(is_active=True)
    for config in configs:
        log = BackupLog.objects.create(config=config)
        run_backup.delay(log.id)
    return f"Triggered {configs.count()} scheduled backups."
