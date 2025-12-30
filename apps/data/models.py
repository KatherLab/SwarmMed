import uuid
from django.db import models
from django.contrib.auth.models import User
from apps.project.models import Project

class ValidationRun(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='validation_runs')
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

class ValidationCheck(models.Model):
    validation_run = models.ForeignKey(ValidationRun, on_delete=models.CASCADE, related_name='checks')
    name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=[
        ('ok', 'OK'),
        ('warning', 'Warning'),
        ('error', 'Error'),
    ])
    message = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True)

class VisualizationRun(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='visualization_runs')
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

class VisualizationPlot(models.Model):
    visualization_run = models.ForeignKey(VisualizationRun, on_delete=models.CASCADE, related_name='plots')
    title = models.CharField(max_length=255)
    plot_number = models.IntegerField()  # 1-4
    image_data = models.TextField()  # Base64 encoded image (PNG)
    svg_data = models.TextField(blank=True, null=True)  # Base64 encoded SVG
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['plot_number']
        unique_together = ['visualization_run', 'plot_number']
