"""
Database models for the training application.
Defines the structure for tracking training jobs in the swarm learning network.
"""


from django.db import models

from common.models import AbstractBaseModel
from network.models import SwarmNetwork
from project.models import Project


class TrainingJob(AbstractBaseModel):
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
        ("PENDING", "Pending"),
        ("STARTING", "Starting"),
        ("RUNNING", "Running"),
        ("COMPLETED", "Completed"),
        ("FAILED", "Failed"),
        ("STOPPED", "Stopped"),
    ]

    # The project this job belongs to (defines what code/data is used).
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="training_jobs"
    )

    # The swarm network (Docker containers/S3) where the job is executed.
    network = models.ForeignKey(
        SwarmNetwork, on_delete=models.CASCADE, related_name="training_jobs"
    )

    # Current execution status.
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="PENDING", db_index=True
    )

    # The job ID returned by NVIDIA FLARE after successful submission.
    # This is used to track logs and results in the NVFlare workspace.
    flare_job_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="The unique job ID assigned by NVIDIA FLARE.",
    )

    # Set manually when the job is detected as finished (COMPLETED, FAILED, or
    # STOPPED).
    completed_at = models.DateTimeField(null=True, blank=True)

    # Progress tracking (updated by periodic Celery monitoring).
    total_rounds = models.PositiveIntegerField(null=True, blank=True)
    rounds_finished = models.PositiveIntegerField(null=True, blank=True)
    progress_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    progress_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)

    def __str__(self):
        """Returns a human-readable string representation of the job."""
        return f"Training Job {self.identifier} for project: {self.project.title}"
