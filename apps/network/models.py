"""
Models for the network app.
Defines the structure for Swarm Learning Networks, participants,
and tracking active networks for users.
"""

import os
import shutil
import subprocess  # nosec
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models

from apps.project.models import Project
from apps.logs.logger import get_logger

logger = get_logger()


class SwarmNetwork(models.Model):
    """
    Represents a Swarm Learning Network configuration.
    This model tracks the provisioning and execution status of
    a decentralized learning setup.
    """
    # Possible states of a swarm network
    STATUS_CHOICES = [
        ('INITIALIZING', 'Initializing'),
        ('PROVISIONED', 'Provisioned'),
        ('STARTING', 'Starting'),
        ('RUNNING', 'Running'),
        ('STOPPING', 'Stopping'),
        ('STOPPED', 'Stopped'),
        ('ERROR', 'Error'),
    ]

    # Display name for the network
    name = models.CharField(max_length=255)

    # Optional detailed description
    description = models.TextField(blank=True, null=True)

    # The project this network belongs to
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='swarm_networks'
    )

    # The user who created this network configuration
    author = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='authored_swarm_networks',
        null=True
    )

    # Current lifecycle status
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='INITIALIZING',
        db_index=True
    )

    # Unique identifier used for file path generation and API calls
    identifier = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True
    )

    # Timestamps for auditing
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        """Returns a string representation of the network."""
        return f"{self.name} for Project {self.project.title}"

    def delete(self, *args, **kwargs):
        """
        Custom delete method to ensure associated Docker resources
        and filesystem directories are cleaned up.
        
        If the network is running, it transitions to STOPPING and
        triggers an async task to stop and then delete the record.
        """
        from .tasks import cleanup_network_resources, stop_and_delete_network_task
        
        # If the network is in a state where it might be running,
        # we don't delete the DB record immediately.
        # Instead, we transition to STOPPING and let the task handle it.
        if self.status in ['RUNNING', 'STARTING', 'STOPPING', 'ERROR']:
            # Avoid re-triggering if already stopping
            if self.status != 'STOPPING':
                self.status = 'STOPPING'
                self.save(update_fields=['status'])
            
            stop_and_delete_network_task.delay(str(self.identifier))
            return

        # For other states (INITIALIZING, PROVISIONED, STOPPED), 
        # trigger async cleanup of any files and delete immediately.
        cleanup_network_resources.delay(
            project_title=self.project.title,
            project_identifier=str(self.project.identifier),
            network_identifier=str(self.identifier),
            network_name=self.name
        )

        # Call the parent class delete method to remove the database record
        super().delete(*args, **kwargs)



class SwarmParticipant(models.Model):
    """
    Represents a participant (server or client) in a Swarm Learning Network.
    Each participant has a specific role and identifier within NVFlare.
    """
    ROLE_CHOICES = [
        ('SERVER', 'Server'),
        ('CLIENT', 'Client'),
    ]

    # The network this participant belongs to
    network = models.ForeignKey(
        SwarmNetwork,
        on_delete=models.CASCADE,
        related_name='participants'
    )

    # The system user associated with this participant
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='swarm_participations'
    )

    # Role in the federated learning setup
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)

    # Internal FLARE name (e.g., 'server', 'client-1')
    participant_id = models.CharField(
        max_length=100,
        help_text="Unique identifier used by FLARE (e.g., 'server', 'client-1')")

    class Meta:
        # Ensure that participant IDs are unique within a specific network
        unique_together = ('network', 'participant_id')
        indexes = [
            # Optimize lookups by user across networks
            models.Index(fields=['user', 'network']),
            models.Index(fields=['network', 'role']),
        ]

    def __str__(self):
        """Returns a string representation of the participant."""
        return (f"{self.user.username} as {self.get_role_display()} "
                f"in {self.network.name}")


class UserCurrentNetwork(models.Model):
    """
    Model to track which network is currently active for a user.
    This allows the UI to remember the user's focus.
    """
    # Each user has exactly one 'current network' record
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='current_network_relation'
    )

    # The specific network currently being focused on
    network = models.ForeignKey(
        SwarmNetwork,
        on_delete=models.SET_NULL,
        null=True,
        related_name='current_for_users'
    )

    class Meta:
        # Extra safety to ensure uniqueness
        unique_together = ('user', 'network')

    def __str__(self):
        """Returns string representation."""
        network_name = self.network.name if self.network else "None"
        return f"Current network for {self.user.username}: {network_name}"
