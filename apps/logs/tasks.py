"""
Celery tasks for system maintenance, including automated backups and data retention purging.
"""

import os
from datetime import timedelta
from django.utils import timezone
from django.core.management import call_command
from django.conf import settings
from django.core.files.storage import default_storage
from celery import shared_task

# Import models to purge
from apps.data.models import ValidationRun, VisualizationRun
from apps.results.models import TrainingResult, ResultsVisualizationRun
from apps.logs.models import LogEntry, LogCategory
from apps.training.models import TrainingJob
from apps.network.models import SwarmNetwork
from apps.users.models import Profile
from apps.logs import logger

@shared_task(name="apps.logs.tasks.scheduled_backup")
def scheduled_backup():
    """
    Triggers the secure_backup management command.
    """
    call_command('secure_backup')

@shared_task(name="apps.logs.tasks.purge_expired_data")
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

    # 1. Purge Validation Runs
    validation_runs = ValidationRun.objects.filter(created_at__lt=cutoff_date)
    val_count = validation_runs.count()
    validation_runs.delete()  # Cascade deletes ValidationCheck

    # 2. Purge Visualization Runs
    visualization_runs = VisualizationRun.objects.filter(created_at__lt=cutoff_date)
    viz_count = visualization_runs.count()
    visualization_runs.delete()  # Cascade deletes VisualizationPlot

    # 3. Purge Training Results
    training_results = TrainingResult.objects.filter(created_at__lt=cutoff_date)
    res_count = training_results.count()
    # Explicitly delete associated files in storage
    for res in training_results:
        if res.file_path:
            try:
                if default_storage.exists(res.file_path):
                    default_storage.delete(res.file_path)
            except Exception as e:
                 # Log error but continue purging other records
                 pass
    training_results.delete()

    # 4. Purge Results Visualization Runs
    res_viz_runs = ResultsVisualizationRun.objects.filter(created_at__lt=cutoff_date)
    res_viz_count = res_viz_runs.count()
    res_viz_runs.delete()

    # 5. Purge Training Jobs
    training_jobs = TrainingJob.objects.filter(created_at__lt=cutoff_date)
    job_count = training_jobs.count()
    training_jobs.delete()

    # 6. Purge Swarm Networks
    swarm_networks = SwarmNetwork.objects.filter(created_at__lt=cutoff_date)
    net_count = swarm_networks.count()
    # Using a loop to ensure the custom delete() method is called for filesystem cleanup
    for net in swarm_networks:
        net.delete()

    # 7. Purge Audit Logs
    # Standard security logs (AUTH, ACCESS, etc.) are purged after 1 year.
    # Logs that might contain PHI metadata (DATA, TRAINING, RESULTS) are kept for 6 years (HIPAA).
    
    standard_categories = [LogCategory.AUTH, LogCategory.ACCESS, LogCategory.PROJECT]
    hipaa_categories = [LogCategory.DATA, LogCategory.TRAINING, LogCategory.RESULTS, LogCategory.NETWORK]
    
    standard_logs = LogEntry.objects.filter(category__in=standard_categories, timestamp__lt=security_cutoff)
    standard_log_count = standard_logs.count()
    standard_logs.delete()
    
    hipaa_logs = LogEntry.objects.filter(category__in=hipaa_categories, timestamp__lt=cutoff_date)
    hipaa_log_count = hipaa_logs.count()
    hipaa_logs.delete()

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

@shared_task(name="apps.logs.tasks.revoke_emergency_access")
def revoke_emergency_access():
    """
    Periodic task to automatically revoke expired emergency access.
    """
    expired_profiles = Profile.objects.filter(
        is_emergency_access=True,
        emergency_access_expiry__lt=timezone.now()
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
            target_user=username
        )
        
    return {"revoked_count": count}

@shared_task(name="apps.logs.tasks.anonymize_security_logs")
def anonymize_security_logs():
    """
    Periodic task to anonymize IP addresses in logs older than 90 days.
    Ensures GDPR compliance for security logging.
    """
    call_command('anonymize_ips')
