import uuid
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from apps.project.models import Project

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
        ]
