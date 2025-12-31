"""
Signal handlers for the project application.
This module listens for database events like pre-delete to trigger
automatic cleanup of related resources.
"""

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.network.models import SwarmNetwork
from .models import Project


@receiver(pre_delete, sender=Project)
def delete_project_cleanup(sender, instance, **kwargs):
    """
    Ensures that when a Project is deleted, all associated Swarm Networks
    are also removed. This prevents 'orphaned' networks and ensures that
    the related Docker containers and files are properly cleaned up.

    Args:
        sender: The model class (Project).
        instance: The actual Project instance being deleted.
        **kwargs: Additional arguments passed by the signal.
    """
    # Find all swarm networks belonging to this specific project.
    swarm_networks = SwarmNetwork.objects.filter(project=instance)

    # Iterate and delete each network.
    # Calling .delete() on each instance triggers the model's custom delete method
    # which handles the Docker cleanup logic and other associated resources.
    for network in swarm_networks:
        network.delete()
