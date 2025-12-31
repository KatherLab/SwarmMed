"""
Models for the logs app.
Defines how log entries are stored in the database, including categories
like project, data, network, training, and results.
"""

import uuid

from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

from apps.network.models import SwarmNetwork


class LogCategory(models.TextChoices):
    """
    Defines the different types of logs we track in the system.
    This helps in filtering and organizing logs for the user.
    """
    PROJECT = 'project', 'Project'
    DATA = 'data', 'Data'
    NETWORK = 'network', 'Network'
    TRAINING = 'training', 'Training'
    RESULTS = 'results', 'Results'


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
        on_delete=models.CASCADE,
        related_name='log_entries'
    )

    # The project this log belongs to
    project = models.ForeignKey(
        'project.Project',
        on_delete=models.CASCADE,
        related_name='log_entries'
    )

    # The category of the log (e.g., Data, Training)
    category = models.CharField(max_length=20, choices=LogCategory.choices)

    # Optional link to a specific swarm network
    swarm_network = models.ForeignKey(
        SwarmNetwork,
        on_delete=models.CASCADE,
        related_name='log_entries',
        null=True,
        blank=True
    )

    # When the event occurred
    timestamp = models.DateTimeField(default=timezone.now)

    # Severity level (e.g., INFO, WARNING, ERROR)
    level = models.CharField(max_length=10, default='INFO')

    # Where the log came from (e.g., a specific client or service)
    source = models.CharField(
        max_length=50,
        blank=True,
        help_text="e.g., overseer, fl-client-1, celery"
    )

    # The actual log message
    message = models.TextField()

    # Additional structured data related to the event
    context_data = models.JSONField(default=dict, blank=True)

    class Meta:
        # Show most recent logs first
        ordering = ['-timestamp']
        # Indexes for faster searching and filtering
        indexes = [
            models.Index(fields=['user', 'project', 'category']),
            models.Index(fields=['timestamp']),
        ]

    def __str__(self):
        """String representation of the log entry."""
        return f"[{self.timestamp}] {self.level} - {self.category}: {self.message[:50]}"
