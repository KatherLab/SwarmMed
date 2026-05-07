"""Database models for the training application.

Defines the structure for tracking training jobs in the swarm learning network.
"""

from django.db.models import Q
from django.db import models

from common.models import AbstractBaseModel
from network.models import SwarmNetwork
from project.models import Project


class TrainingJob(AbstractBaseModel):
    """Tracks the status, configuration, and progress of a swarm learning training job.

    A job is linked to a project (the task) and a network (the infrastructure).

    Attributes:
        project (ForeignKey): The project this job belongs to.
        network (ForeignKey): The swarm network where the job is executed.
        status (CharField): Current execution status.
        flare_job_id (CharField): The unique job ID assigned by NVIDIA FLARE.
        completed_at (DateTimeField): When the job was finished.
        total_rounds (PositiveIntegerField): Total number of training rounds.
        rounds_finished (PositiveIntegerField): Number of rounds completed.
        progress_percent (PositiveSmallIntegerField): Percentage of progress.
        progress_updated_at (DateTimeField): When progress was last updated.
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

    # Canonical UUID extracted from the raw FLARE job identifier payload.
    flare_job_uuid = models.CharField(
        max_length=36,
        blank=True,
        null=True,
        db_index=True,
        help_text="Canonical NVFlare job UUID extracted from flare_job_id.",
    )

    # Set manually when the job is detected as finished (COMPLETED, FAILED, or
    # STOPPED).
    completed_at = models.DateTimeField(null=True, blank=True)

    # Progress tracking (updated by periodic Celery monitoring).
    total_rounds = models.PositiveIntegerField(null=True, blank=True)
    rounds_finished = models.PositiveIntegerField(null=True, blank=True)
    progress_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    progress_updated_at = models.DateTimeField(
        null=True, blank=True, db_index=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["network", "flare_job_uuid"],
                condition=Q(flare_job_uuid__isnull=False),
                name="training_unique_flare_uuid_per_network",
            )
        ]

    def __str__(self):
        """Returns a human-readable string representation of the job.

        Returns:
            str: Description of the training job.
        """
        return (
            f"Training Job {self.identifier} for project: {self.project.title}"
        )
