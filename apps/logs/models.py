"""
Models for the logs app.
Defines how log entries are stored in the database, including categories
like project, data, network, training, and results.
"""

import hashlib
import hmac
import uuid

from common.fields import EncryptedTextField
from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class LogCategory(models.TextChoices):
    """
    Defines the different types of logs we track in the system.
    This helps in filtering and organizing logs for the user.
    """

    PROJECT = "project", "Project"
    DATA = "data", "Data"
    NETWORK = "network", "Network"
    TRAINING = "training", "Training"
    RESULTS = "results", "Results"


class LogSigningKey(models.Model):
    """
    Stores keys used for signing log entries.
    Allows for key rotation while maintaining the ability to verify old logs.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=255, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def get_active_key(cls):
        """
        Retrieves the currently active signing key or creates one if none exists.
        Uses Django cache to avoid repeated database lookups.
        """
        from django.core.cache import cache

        cache_key = "active_log_signing_key_id"
        key_id = cache.get(cache_key)

        if key_id:
            try:
                return cls.objects.get(id=key_id)
            except cls.DoesNotExist:
                pass

        active_key = cls.objects.filter(is_active=True).first()
        if not active_key:
            import secrets
            import string

            alphabet = string.ascii_letters + string.digits
            new_key = "".join(secrets.choice(alphabet) for _ in range(64))
            active_key = cls.objects.create(key=new_key)

        # Cache the key ID for 1 hour to reduce DB load
        cache.set(cache_key, active_key.id, 3600)
        return active_key


class LogEntry(models.Model):
    """
    Represents a single log event in the system.
    Stores metadata like user, project, category, and the actual message.
    """

    # Unique identifier for the log entry
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The user who performed the action or triggered the log
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="log_entries",
        null=True,
        blank=True,
    )

    # Stores the username at the time of log creation for audit trail persistence
    # even after user deletion.
    user_identifier = models.CharField(
        max_length=150, blank=True, null=True, editable=False
    )

    # The project this log belongs to
    project = models.ForeignKey(
        "project.Project",
        on_delete=models.CASCADE,
        related_name="log_entries",
        null=True,
        blank=True,
    )

    # The category of the log (e.g., Data, Training)
    category = models.CharField(max_length=20, choices=LogCategory.choices)

    # Optional link to a specific swarm network
    swarm_network = models.ForeignKey(
        "network.SwarmNetwork",
        on_delete=models.CASCADE,
        related_name="log_entries",
        null=True,
        blank=True,
    )

    # When the event occurred
    timestamp = models.DateTimeField(default=timezone.now)

    # Severity level (e.g., INFO, WARNING, ERROR, CRITICAL)
    level = models.CharField(max_length=10, default="INFO")

    # Where the log originated (e.g., 'web', 'celery', or a specific container name)
    source = models.CharField(max_length=100, default="web")

    # The actual log message (Encrypted)
    message = EncryptedTextField()

    # Additional context for the log entry
    context_data = models.JSONField(blank=True, default=dict)

    # Metadata from the request context
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    path = models.CharField(max_length=255, null=True, blank=True)
    object_id = models.CharField(max_length=255, null=True, blank=True)

    # Cryptographic fields for tamper-evidence
    signing_key = models.ForeignKey(
        LogSigningKey,
        on_delete=models.PROTECT,
        related_name="signed_entries",
        null=True,
        blank=True,
    )
    previous_hash = models.CharField(max_length=128, blank=True, null=True)
    signature = models.CharField(max_length=128, blank=True, null=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name_plural = "Log Entries"
        indexes = [
            models.Index(fields=["user", "project", "category"]),
            models.Index(fields=["timestamp"]),
        ]

    def __str__(self):
        return f"[{self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}] {self.category} - {self.level}: {self.message[:50]}"

    def calculate_signature(self, key_obj=None):
        """Calculates a SHA-256 HMAC signature of the log entry.
        Includes timestamp, user_id, category, message, and previous_hash.
        Uses a combination of the LogSigningKey (database) and settings.SECRET_KEY (environment).
        """
        if not key_obj:
            key_obj = self.signing_key or LogSigningKey.get_active_key()

        user_id = str(self.user.id) if self.user else "system"
        ts_str = self.timestamp.isoformat()

        # Data to sign
        data = f"{self.id}{ts_str}{user_id}{self.category}{self.message}{self.previous_hash}"

        # Combine database-stored key with environment-stored SECRET_KEY
        combined_key = f"{key_obj.key}{settings.SECRET_KEY}".encode()
        signature = hmac.new(
            combined_key, data.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return signature

    def save(self, *args, **kwargs):
        """Override save to generate signature and link to previous log."""
        if not self.user_identifier and self.user:
            self.user_identifier = self.user.username

        if not self.signature:
            # Assign active signing key
            if not self.signing_key:
                self.signing_key = LogSigningKey.get_active_key()

            # Find the most recent log entry to chain
            # Optimization: Use Redis to cache the latest signature to avoid DB lookup
            from django.core.cache import cache
            cache_key = "latest_log_signature"
            previous_signature = cache.get(cache_key)

            if not previous_signature:
                last_entry = LogEntry.objects.order_by("-timestamp").first()
                if last_entry:
                    previous_signature = last_entry.signature
                else:
                    previous_signature = "0" * 64  # Genesis block
            
            self.previous_hash = previous_signature
            self.signature = self.calculate_signature()
            
            # Update cache with the new signature
            cache.set(cache_key, self.signature, 3600 * 24) # Cache for 24h

        super().save(*args, **kwargs)
