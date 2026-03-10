"""
Models for the network app.
Defines the structure for Swarm Learning Networks, participants,
and tracking active networks for users.
"""

from common.models import AbstractBaseModel
from django.contrib.auth.models import User
from django.db import models
from logs.logger import get_logger
from project.models import Project

logger = get_logger()


class SwarmNetwork(AbstractBaseModel):
    """
    Represents a Swarm Learning Network configuration.
    This model tracks the provisioning and execution status of
    a decentralized learning setup.
    """

    # Possible states of a swarm network
    STATUS_CHOICES = [
        ("INITIALIZING", "Initializing"),
        ("PROVISIONED", "Provisioned"),
        ("STARTING", "Starting"),
        ("RUNNING", "Running"),
        ("STOPPING", "Stopping"),
        ("STOPPED", "Stopped"),
        ("ERROR", "Error"),
    ]

    # Display name for the network
    name = models.CharField(max_length=255)

    # Optional detailed description
    description = models.TextField(blank=True, null=True)

    # The project this network belongs to
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="swarm_networks"
    )

    # The user who created this network configuration
    author = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="authored_swarm_networks",
        null=True,
    )

    # Current lifecycle status
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="INITIALIZING",
        db_index=True,
    )

    # Optional path to admin startup kit for decentralized NVFlare polling
    admin_startup_dir = models.CharField(
        max_length=512,
        blank=True,
        null=True,
    )

    # NEW: Secure gossip token for peer-to-peer status sync
    gossip_token = models.CharField(
        max_length=64,
        blank=True,
        null=True,
        help_text="Shared secret for authenticating gossip status shouts."
    )

    @staticmethod
    def resolve_current(user) -> 'SwarmNetwork':
        """
        Heuristic to find the most relevant network for the user's current project context.
        Prioritizes:
        1. User's manually selected network (UserCurrentNetwork).
        2. Any network in the project that is currently 'RUNNING'.
        3. The most recently created 'PROVISIONED' network in the project.
        """
        from project.models import UserCurrentProject
        try:
            current_project_rel = UserCurrentProject.objects.get(user=user)
            current_project = current_project_rel.project
            if not current_project:
                return None
        except UserCurrentProject.DoesNotExist:
            return None

        # 1. Check for a manual selection
        current_network = None
        try:
            rel = UserCurrentNetwork.objects.get(user=user)
            if rel.network and rel.network.project == current_project:
                current_network = rel.network
        except UserCurrentNetwork.DoesNotExist:
            pass

        # 2. If no selection or selection is not RUNNING, look for a RUNNING one
        if not current_network or current_network.status != "RUNNING":
            active = SwarmNetwork.objects.filter(
                project=current_project, status="RUNNING"
            ).order_by("-created_at").first()
            if active:
                return active

        # 3. Fallback to selection or any provisioned network
        if not current_network or (current_network and current_network.status != "PROVISIONED"):
            provisioned = SwarmNetwork.objects.filter(
                project=current_project, status="PROVISIONED"
            ).order_by("-created_at").first()
            if provisioned:
                return provisioned

        return current_network

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
        from .tasks import (
            cleanup_network_resources,
            stop_and_delete_network_task,
        )

        # If the network is in a state where it might be running,
        # we don't delete the DB record immediately.
        # Instead, we transition to STOPPING and let the task handle it.
        if self.status in ["RUNNING", "STARTING", "STOPPING", "ERROR"]:
            # Avoid re-triggering if already stopping
            if self.status != "STOPPING":
                self.status = "STOPPING"
                self.save(update_fields=["status"])

            stop_and_delete_network_task.delay(str(self.identifier))
            return

        # For other states (INITIALIZING, PROVISIONED, STOPPED),
        # trigger async cleanup of any files and delete immediately.
        cleanup_network_resources.delay(
            project_title=self.project.title,
            project_identifier=str(self.project.identifier),
            network_identifier=str(self.identifier),
            network_name=self.name,
        )

        # Call the parent class delete method to remove the database record
        super().delete(*args, **kwargs)


class SwarmParticipant(models.Model):
    """
    Represents a participant (server or client) in a Swarm Learning Network.
    Each participant has a specific role and identifier within NVFlare.
    """

    ROLE_CHOICES = [
        ("SERVER", "Server"),
        ("CLIENT", "Client"),
    ]

    # The network this participant belongs to
    network = models.ForeignKey(
        SwarmNetwork, on_delete=models.CASCADE, related_name="participants"
    )

    # The system user associated with this participant
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="swarm_participations"
    )

    # Role in the federated learning setup
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)

    # Internal FLARE name (e.g., 'server', 'client-1')
    participant_id = models.CharField(
        max_length=100,
        help_text="Unique identifier used by FLARE (e.g., 'server', 'client-1')",
    )

    # Organization name
    org = models.CharField(max_length=255, blank=True, null=True)

    # IP address or hostname
    ip = models.CharField(max_length=255, blank=True, null=True)

    # NEW: Gossip Status
    status = models.CharField(max_length=50, default="OFFLINE")
    last_seen = models.DateTimeField(null=True, blank=True)

    class Meta:
        # Ensure that participant IDs are unique within a specific network
        unique_together = ("network", "participant_id")
        indexes = [
            # Optimize lookups by user across networks
            models.Index(fields=["user", "network"]),
            models.Index(fields=["network", "role"]),
        ]

    def __str__(self):
        """Returns a string representation of the participant."""
        return f"{self.user.username} as {self.get_role_display()} in {self.network.name}"


class UserCurrentNetwork(AbstractBaseModel):
    """
    Model to track which network is currently active for a user.
    This allows the UI to remember the user's focus.
    """

    # Each user has exactly one 'current network' record
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="current_network_relation"
    )

    # The specific network currently being focused on
    network = models.ForeignKey(
        SwarmNetwork,
        on_delete=models.SET_NULL,
        null=True,
        related_name="current_for_users",
    )

    class Meta:
        # Extra safety to ensure uniqueness
        unique_together = ("user", "network")

    def __str__(self):
        """Returns string representation."""
        network_name = self.network.name if self.network else "None"
        return f"Current network for {self.user.username}: {network_name}"
