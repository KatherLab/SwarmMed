from django.db import models
from django.contrib.auth.models import User
from apps.project.models import Project
import uuid
import os
import shutil

class SwarmNetwork(models.Model):
    """
    Represents a Swarm Learning Network configuration.
    """
    STATUS_CHOICES = [
        ('INITIALIZING', 'Initializing'),
        ('PROVISIONED', 'Provisioned'),
        ('RUNNING', 'Running'),
        ('STOPPED', 'Stopped'),
        ('ERROR', 'Error'),
    ]

    name = models.CharField(max_length=255)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='swarm_networks')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='INITIALIZING')
    identifier = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} for Project {self.project.title}"

    def delete(self, *args, **kwargs):
        # Clean up the provisioning directory before deleting the object
        provision_dir = os.path.join('workspaces', str(self.project.identifier), 'provision', str(self.identifier))
        if os.path.exists(provision_dir):
            shutil.rmtree(provision_dir)
            print(f"Deleted provisioning directory: {provision_dir}")
        super().delete(*args, **kwargs)

class SwarmParticipant(models.Model):
    """
    Represents a participant (server or client) in a Swarm Learning Network.
    """
    ROLE_CHOICES = [
        ('SERVER', 'Server'),
        ('CLIENT', 'Client'),
    ]

    network = models.ForeignKey(SwarmNetwork, on_delete=models.CASCADE, related_name='participants')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='swarm_participations')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    participant_id = models.CharField(max_length=100, help_text="Unique identifier used by FLARE (e.g., 'server', 'client-1')")

    class Meta:
        unique_together = ('network', 'participant_id')

    def __str__(self):
        return f"{self.user.username} as {self.get_role_display()} in {self.network.name}"