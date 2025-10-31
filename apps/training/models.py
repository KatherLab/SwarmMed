from django.db import models
from apps.project.models import Project
from apps.network.models import SwarmNetwork
import uuid

class TrainingJob(models.Model):
    """
    Tracks the status and configuration of a swarm learning training job.
    """
    STATUS_CHOICES = [
        ('PENDING', 'Pending'),
        ('STARTING', 'Starting'),
        ('RUNNING', 'Running'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='training_jobs')
    network = models.ForeignKey(SwarmNetwork, on_delete=models.CASCADE, related_name='training_jobs')
    identifier = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    flare_job_id = models.CharField(max_length=255, blank=True, null=True, help_text="The job ID from NVIDIA FLARE")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Training Job {self.identifier} for {self.project.title}"