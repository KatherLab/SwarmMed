import uuid
from django.db import models
from django.contrib.auth.models import User
from apps.project.models import Project
from apps.training.models import TrainingJob


class TrainingResult(models.Model):
    """
    Represents a single result file from a training job.
    """
    job = models.ForeignKey(TrainingJob, on_delete=models.CASCADE, related_name='results')
    identifier = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    file_path = models.CharField(max_length=1024)
    file_size = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Result {self.identifier} for job {self.job.identifier}"


class ResultsVisualizationRun(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='results_visualization_runs')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Results
    success = models.BooleanField(null=True, blank=True)
    output = models.TextField(blank=True)
    error_message = models.TextField(blank=True)

    # Celery task ID for cancellation
    celery_task_id = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-created_at']


class ResultsVisualizationPlot(models.Model):
    visualization_run = models.ForeignKey(ResultsVisualizationRun, on_delete=models.CASCADE, related_name='plots')
    title = models.CharField(max_length=255)
    plot_number = models.IntegerField()  # 1-4
    image_data = models.TextField()  # Base64 encoded image (PNG)
    svg_data = models.TextField(blank=True, null=True)  # SVG string
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['plot_number']
        unique_together = ['visualization_run', 'plot_number']
