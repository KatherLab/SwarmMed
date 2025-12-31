"""
Database models for the training application.
Defines the structure for tracking training jobs in the swarm learning network.
"""

import uuid

from django.db import models

from apps.network.models import SwarmNetwork
from apps.project.models import Project


class TrainingJob(models.Model):
    """
    Tracks the status, configuration, and progress of a swarm learning training job.
    A job is linked to a project (the task) and a network (the infrastructure).
    """

    # Possible states for a training job.
    # PENDING: Created in DB but not yet submitted.
    # STARTING: Submission is in progress.
    # RUNNING: The job is active in the NVFlare system.
    # COMPLETED: Training finished successfully.
    # FAILED: An error occurred during training or submission.
    # STOPPED: Manually aborted by the user.
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('STARTING', 'Starting'),
        ('RUNNING', 'Running'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('STOPPED', 'Stopped'),
    ]

    # The project this job belongs to (defines what code/data is used).
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='training_jobs'
    )

    # The swarm network (Docker containers/S3) where the job is executed.
    network = models.ForeignKey(
        SwarmNetwork,
        on_delete=models.CASCADE,
        related_name='training_jobs'
    )

    # A unique internal identifier for this job entry.
    identifier = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True
    )

    # Current execution status.
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='PENDING'
    )

    # The job ID returned by NVIDIA FLARE after successful submission.
    # This is used to track logs and results in the NVFlare workspace.
    flare_job_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="The unique job ID assigned by NVIDIA FLARE."
    )

    # Automatically set when the record is created.
    created_at = models.DateTimeField(auto_now_add=True)

    # Set manually when the job is detected as finished (COMPLETED, FAILED, or
    # STOPPED).
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        """Returns a human-readable string representation of the job."""
        return f"Training Job {self.identifier} for project: {self.project.title}"
