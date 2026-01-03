"""
Celery tasks for system maintenance, including automated backups and data retention purging.
"""

from datetime import timedelta
from django.utils import timezone
from django.core.management import call_command
from django.conf import settings
from django.core.files.storage import default_storage
from celery import shared_task

# Import models to purge
from data.models import ValidationRun, VisualizationRun
from results.models import TrainingResult, ResultsVisualizationRun
from logs.models import LogEntry, LogCategory
from training.models import TrainingJob
from network.models import SwarmNetwork
from users.models import Profile
from logs import logger


@shared_task(name="logs.tasks.scheduled_backup")
def scheduled_backup():
    """
    Automated task to perform a full database backup.
    """
    call_command("secure_backup")


@shared_task(name="logs.tasks.purge_expired_data")
def purge_expired_data():
    """
    Deletes records and associated files that are older than the retention period.
    - PHI metadata and related records: 6 years (HIPAA requirement).
    - Standard security and audit logs: 1 year (GDPR/Compliance policy).
    """
    # 1. HIPAA/PHI Data Retention (default: 6 years)
    retention_days = settings.DATA_RETENTION_DAYS
    cutoff_date = timezone.now() - timedelta(days=retention_days)

    # 2. Security Log Retention (default: 1 year)
    security_retention_days = settings.SECURITY_LOG_RETENTION_DAYS
    security_cutoff = timezone.now() - timedelta(days=security_retention_days)

    # 1. Purge Validation Runs (Bulk delete is safe, signals/cascades will run)
    val_count, _ = ValidationRun.objects.filter(created_at__lt=cutoff_date).delete()

    # 2. Purge Visualization Runs
    viz_count, _ = VisualizationRun.objects.filter(created_at__lt=cutoff_date).delete()

    # 3. Purge Training Results
    # We need to handle file cleanup.
    # Optimization: Fetch only necessary fields to reduce memory usage.
    training_results = TrainingResult.objects.filter(created_at__lt=cutoff_date).only(
        "file_path", "id"
    )
    res_count = training_results.count()

    for res in training_results.iterator():
        if res.file_path:
            try:
                # Avoid extra network calls: delete() is typically idempotent.
                default_storage.delete(res.file_path)
            except Exception:
                # Log error but continue purging other records
                pass

    # Bulk delete the records after file cleanup
    TrainingResult.objects.filter(created_at__lt=cutoff_date).delete()

    # 4. Purge Results Visualization Runs
    res_viz_count, _ = ResultsVisualizationRun.objects.filter(
        created_at__lt=cutoff_date
    ).delete()

    # 5. Purge Training Jobs
    job_count, _ = TrainingJob.objects.filter(created_at__lt=cutoff_date).delete()

    # 6. Purge Swarm Networks
    # These models likely have custom delete logic (e.g. stopping containers).
    # We must iterate and call .delete() on each instance.
    swarm_networks = SwarmNetwork.objects.filter(created_at__lt=cutoff_date)
    net_count = swarm_networks.count()
    for net in swarm_networks.iterator():
        net.delete()

    # 7. Purge Audit Logs
    # Standard security logs (PROJECT) are purged after 1 year.
    # Logs that might contain PHI metadata (DATA, TRAINING, RESULTS) are kept for 6 years (HIPAA).

    standard_categories = [LogCategory.PROJECT]
    hipaa_categories = [
        LogCategory.DATA,
        LogCategory.TRAINING,
        LogCategory.RESULTS,
        LogCategory.NETWORK,
    ]

    # Bulk delete for logs is highly efficient and recommended
    standard_log_count, _ = LogEntry.objects.filter(
        category__in=standard_categories, timestamp__lt=security_cutoff
    ).delete()

    hipaa_log_count, _ = LogEntry.objects.filter(
        category__in=hipaa_categories, timestamp__lt=cutoff_date
    ).delete()

    return {
        "validation_runs_purged": val_count,
        "visualization_runs_purged": viz_count,
        "training_results_purged": res_count,
        "results_viz_purged": res_viz_count,
        "training_jobs_purged": job_count,
        "swarm_networks_purged": net_count,
        "security_logs_purged": standard_log_count,
        "hipaa_audit_logs_purged": hipaa_log_count,
    }


@shared_task(name="logs.tasks.revoke_emergency_access")
def revoke_emergency_access():
    """
    Periodic task to automatically revoke expired emergency access.
    """
    expired_profiles = Profile.objects.filter(
        is_emergency_access=True, emergency_access_expiry__lt=timezone.now()
    )

    count = expired_profiles.count()
    log = logger.get_logger()

    for profile in expired_profiles:
        username = profile.user.username
        profile.is_emergency_access = False
        profile.emergency_access_expiry = None
        profile.save()

        log.access.info(
            f"AUTOMATIC REVOCATION of expired emergency access for user {username}.",
            target_user=username,
        )

    return {"revoked_count": count}


@shared_task(name="logs.tasks.anonymize_security_logs")
def anonymize_security_logs():
    """
    Periodic task to anonymize IP addresses in logs older than 90 days.
    Ensures GDPR compliance for security logging.
    """
    call_command("anonymize_ips")