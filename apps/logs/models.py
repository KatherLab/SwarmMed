import uuid
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from ..project.models import Project

class LogCategory(models.TextChoices):
    PROJECT = 'project', 'Project'
    DATA = 'data', 'Data'
    NETWORK = 'network', 'Network'
    TRAINING = 'training', 'Training'
    RESULTS = 'results', 'Results'

class LogEntry(models.Model):
    """Model to track individual log entries"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='log_entries')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='log_entries')
    category = models.CharField(max_length=20, choices=LogCategory.choices)
    
    # Log metadata
    session_id = models.UUIDField(default=uuid.uuid4)  # Group related log entries
    timestamp = models.DateTimeField(default=timezone.now)
    level = models.CharField(max_length=10, default='INFO')
    
    # Log content
    message = models.TextField()
    context_data = models.JSONField(default=dict, blank=True)  # Additional context
    
    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['user', 'project', 'category']),
            models.Index(fields=['timestamp']),
            models.Index(fields=['session_id']),
        ]

class LogSession(models.Model):
    """Model to track logging sessions (e.g., a training run, data processing job)"""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    category = models.CharField(max_length=20, choices=LogCategory.choices)
    
    name = models.CharField(max_length=255)  # e.g., "Training Run #1", "Data Validation"
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default='running')  # running, completed, failed
    
    class Meta:
        ordering = ['-started_at']
