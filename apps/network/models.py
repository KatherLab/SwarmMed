"""
Models for the network app.
Defines the structure for Swarm Learning Networks, participants,
and tracking active networks for users.
"""

import os
import shutil
import subprocess
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models

from apps.project.models import Project


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
        default='INITIALIZING'
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
        """
        # 1. If the network is running, stop and remove containers
        if self.status == 'RUNNING':
            project_name = self.project.title.replace(' ', '_')
            provision_dir = os.path.join(
                settings.BASE_DIR,
                'workspaces',
                str(self.project.identifier),
                str(self.identifier)
            )
            compose_dir = os.path.join(
                provision_dir,
                'workspace',
                project_name,
                'prod_00'
            )
            compose_file_path = os.path.join(compose_dir, 'compose.yaml')

            if os.path.exists(compose_file_path):
                # Handle path mapping if running inside a container (e.g.,
                # Docker-in-Docker)
                host_project_path = os.getenv('HOST_PROJECT_PATH')
                if host_project_path:
                    with open(compose_file_path, 'r') as f:
                        compose_content = f.read()

                    relative_compose_dir = os.path.relpath(
                        compose_dir,
                        settings.BASE_DIR
                    )
                    host_compose_dir = os.path.join(
                        host_project_path,
                        relative_compose_dir
                    )

                    # Update paths to point to host machine directories
                    mappings = {
                        'build: ./nvflare': f'build: {os.path.join(host_compose_dir, "nvflare")}',
                        './fl-client': os.path.join(host_compose_dir, 'fl-client'),
                        './server': os.path.join(host_compose_dir, 'server'),
                        './overseer': os.path.join(host_compose_dir, 'overseer'),
                    }
                    for old, new in mappings.items():
                        compose_content = compose_content.replace(old, new)

                    with open(compose_file_path, 'w') as f:
                        f.write(compose_content)

                # Stop and remove containers via docker-compose
                subprocess.run(
                    ['docker-compose', '-f', 'compose.yaml', 'down'],
                    cwd=compose_dir
                )

        # 2. Clean up the provisioning directory on the filesystem
        provision_dir = os.path.join(
            'workspaces',
            str(self.project.identifier),
            str(self.identifier)
        )
        if os.path.exists(provision_dir):
            shutil.rmtree(provision_dir)

        # 3. Call the parent class delete method to remove the database record
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
