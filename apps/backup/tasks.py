"""Celery tasks for the backup app.

This module contains asynchronous tasks for running backups, restores,
and scheduled backup operations.
"""

from celery import shared_task

from .models import BackupConfiguration, BackupLog
from .utils import perform_backup, perform_restore


@shared_task(name="backup.tasks.run_backup")
def run_backup(log_id):
    """Background task to execute a system backup.

    Args:
        log_id (int): The ID of the BackupLog instance to process.

    Returns:
        str: A message indicating the result of the backup operation.
    """
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
    """Background task to execute a system restore from a backup.

    Args:
        log_id (int): The ID of the BackupLog instance containing the backup to restore.

    Returns:
        str: A message indicating the result of the restore operation.
    """
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
    """Background task to trigger backups for all active configurations.

    Returns:
        str: A summary message of the triggered backups.
    """
    configs = BackupConfiguration.objects.filter(is_active=True)
    for config in configs:
        log = BackupLog.objects.create(config=config)
        run_backup.delay(log.id)
    return f"Triggered {configs.count()} scheduled backups."
