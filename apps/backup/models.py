"""Database models for the backup app.

This module defines the data structures for backup configurations and logs,
enabling the tracking of backup settings and historical backup events.
"""

from django.db import models

from common.models import AbstractBaseModel


class StorageBackend(models.TextChoices):
    """Enumeration of supported backup storage backends."""

    LOCAL = "local", "Local Filesystem"
    S3 = "s3", "S3 / MinIO Storage"


class BackupStatus(models.TextChoices):
    """Enumeration of possible backup and restore operation statuses."""

    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"
    RESTORING = "restoring", "Restoring"


class BackupConfiguration(AbstractBaseModel):
    """Global configuration for system-wide backups.

    Stores settings for storage targets, included data components, and retention policies.

    Attributes:
        name (CharField): Name of the backup configuration.
        storage_backend (CharField): Target storage backend for backups.
        local_path (CharField): Local directory path if storage is Local.
        s3_bucket (CharField): S3 Bucket name.
        s3_prefix (CharField): Prefix/Folder within the bucket.
        s3_endpoint_url (CharField): Custom S3 Endpoint URL.
        s3_access_key_id (CharField): Custom S3 Access Key ID.
        s3_secret_access_key (CharField): Custom S3 Secret Access Key.
        s3_region_name (CharField): Custom S3 Region.
        include_databases (BooleanField): Whether to include databases in the backup.
        include_media (BooleanField): Whether to include media files in the backup.
        include_s3_storage (BooleanField): Include all objects from the default S3 bucket.
        is_active (BooleanField): Whether this configuration is currently active.
        retention_days (PositiveIntegerField): Number of days to keep backup logs and files.
    """

    name = models.CharField(
        max_length=255,
        default="Default Configuration",
        help_text="Name of the backup configuration.",
    )
    storage_backend = models.CharField(
        max_length=20,
        choices=StorageBackend.choices,
        default=StorageBackend.S3,
        help_text="Target storage backend for backups.",
    )
    local_path = models.CharField(
        max_length=512,
        blank=True,
        help_text="Local directory path if storage is Local.",
    )

    # Custom S3 Instance Settings (Optional, defaults to system S3 if empty)
    s3_bucket = models.CharField(
        max_length=255, blank=True, help_text="S3 Bucket name."
    )
    s3_prefix = models.CharField(
        max_length=255,
        default="backups/",
        help_text="Prefix/Folder within the bucket.",
    )
    s3_endpoint_url = models.CharField(
        max_length=512,
        blank=True,
        help_text="Custom S3 Endpoint (e.g., https://s3.amazonaws.com).",
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

    include_databases = models.BooleanField(
        default=True, help_text="Whether to include databases in the backup."
    )
    include_media = models.BooleanField(
        default=True, help_text="Whether to include media files in the backup."
    )
    include_s3_storage = models.BooleanField(
        default=True,
        help_text="Include all objects from the default S3/MinIO bucket.",
    )

    is_active = models.BooleanField(
        default=True, help_text="Whether this configuration is currently active."
    )
    retention_days = models.PositiveIntegerField(
        default=30, help_text="Number of days to keep backup logs and files."
    )

    class Meta:
        """Meta options for the BackupConfiguration model."""

        verbose_name = "Backup Configuration"
        verbose_name_plural = "Backup Configurations"

    def __str__(self):
        """Returns the string representation of the backup configuration.

        Returns:
            str: The name of the configuration.
        """
        return self.name


class BackupLog(AbstractBaseModel):
    """Tracks individual backup files and their execution status.

    Stores metadata about what was backed up, when, and where it is stored.

    Attributes:
        config (ForeignKey): The configuration used for this backup.
        status (CharField): Current status of the backup operation.
        filename (CharField): Name of the generated backup file.
        file_size (BigIntegerField): Size of the backup file in bytes.
        storage_location (CharField): Path or URL to the stored backup file.
        started_at (DateTimeField): When the backup operation started.
        finished_at (DateTimeField): When the backup operation finished.
        error_message (TextField): Error message if the backup failed.
        included_databases (JSONField): List of database aliases included in the backup.
        has_media (BooleanField): Whether media files were included.
        has_s3_storage (BooleanField): Whether S3 storage was included.
    """

    config = models.ForeignKey(
        BackupConfiguration,
        on_delete=models.CASCADE,
        related_name="logs",
        help_text="The configuration used for this backup.",
    )
    status = models.CharField(
        max_length=20,
        choices=BackupStatus.choices,
        default=BackupStatus.PENDING,
        help_text="Current status of the backup operation.",
    )

    filename = models.CharField(
        max_length=255, blank=True, help_text="Name of the generated backup file."
    )
    file_size = models.BigIntegerField(
        null=True, blank=True, help_text="Size of the backup file in bytes."
    )
    storage_location = models.CharField(
        max_length=1024, blank=True, help_text="Path or URL to the stored backup file."
    )

    started_at = models.DateTimeField(
        auto_now_add=True, help_text="When the backup operation started."
    )
    finished_at = models.DateTimeField(
        null=True, blank=True, help_text="When the backup operation finished."
    )

    error_message = models.TextField(
        blank=True, help_text="Error message if the backup failed."
    )

    # Metadata about what was backed up
    included_databases = models.JSONField(
        default=list, help_text="List of database aliases included in the backup."
    )
    has_media = models.BooleanField(
        default=False, help_text="Whether media files were included."
    )
    has_s3_storage = models.BooleanField(
        default=False, help_text="Whether S3 storage was included."
    )

    class Meta:
        """Meta options for the BackupLog model."""

        ordering = ["-started_at"]
        verbose_name = "Backup Log"
        verbose_name_plural = "Backup Logs"

    def __str__(self):
        """Returns the string representation of the backup log.

        Returns:
            str: Identifier and status of the backup log.
        """
        return f"Backup {self.identifier} - {self.status}"
