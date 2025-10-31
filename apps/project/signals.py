from django.db.models.signals import pre_delete
from django.dispatch import receiver
from .models import Project
from apps.network.models import SwarmNetwork

@receiver(pre_delete, sender=Project)
def delete_project_cleanup(sender, instance, **kwargs):
    """
    Signal handler to clean up resources associated with a Project before it's deleted.
    """
    swarm_networks = SwarmNetwork.objects.filter(project=instance)
    for network in swarm_networks:
        network.delete()
