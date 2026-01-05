import uuid
from django.db import models
from django.utils import timezone
from common.models import AbstractBaseModel

class StorageBackend(models.TextChoices):
    LOCAL = "local", "Local Filesystem"
    S3 = "s3", "S3 / MinIO Storage"

class BackupStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"
    RESTORING = "restoring", "Restoring"

class BackupConfiguration(AbstractBaseModel):
    """
    Global configuration for backups.
    """
    name = models.CharField(max_length=255, default="Default Configuration")
    storage_backend = models.CharField(
        max_length=20, choices=StorageBackend.choices, default=StorageBackend.S3
    )
    local_path = models.CharField(
        max_length=512, blank=True, help_text="Local directory path if storage is Local."
    )
    
    # Custom S3 Instance Settings (Optional, defaults to system S3 if empty)
    s3_bucket = models.CharField(
        max_length=255, blank=True, help_text="S3 Bucket name."
    )
    s3_prefix = models.CharField(
        max_length=255, default="backups/", help_text="Prefix/Folder within the bucket."
    )
    s3_endpoint_url = models.CharField(
        max_length=512, blank=True, help_text="Custom S3 Endpoint (e.g., https://s3.amazonaws.com)."
    )
    s3_access_key_id = models.CharField(
        max_length=255, blank=True, help_text="Custom S3 Access Key ID."
    )
    s3_secret_access_key = models.CharField(
        max_length=255, blank=True, help_text="Custom S3 Secret Access Key."
    )
    s3_region_name = models.CharField(
        max_length=100, blank=True, help_text="Custom S3 Region."
    )
    
    include_databases = models.BooleanField(default=True)
    include_media = models.BooleanField(default=True)
    include_s3_storage = models.BooleanField(
        default=True, help_text="Include all objects from the default S3/MinIO bucket."
    )
    
    is_active = models.BooleanField(default=True)
    retention_days = models.PositiveIntegerField(default=30)
    
    class Meta:
        verbose_name = "Backup Configuration"
        verbose_name_plural = "Backup Configurations"

    def __str__(self):
        return self.name

class BackupLog(AbstractBaseModel):
    """
    Tracks individual backup files and their status.
    """
    config = models.ForeignKey(BackupConfiguration, on_delete=models.CASCADE, related_name="logs")
    status = models.CharField(max_length=20, choices=BackupStatus.choices, default=BackupStatus.PENDING)
    
    filename = models.CharField(max_length=255, blank=True)
    file_size = models.BigIntegerField(null=True, blank=True)
    storage_location = models.CharField(max_length=1024, blank=True)
    
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    
    error_message = models.TextField(blank=True)
    
    # Metadata about what was backed up
    included_databases = models.JSONField(default=list)
    has_media = models.BooleanField(default=False)
    has_s3_storage = models.BooleanField(default=False)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Backup Log"
        verbose_name_plural = "Backup Logs"

    def __str__(self):
        return f"Backup {self.identifier} - {self.status}"
