"""
Signal handlers for the users application.
Listens for database events related to the User model to automatically
manage associated Profile instances.
"""

from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """
    Automatically creates a Profile instance whenever a new User is created.
    """
    if created:
        if instance.is_superuser:
            # Superusers are automatically granted the 'admin' role.
            Profile.objects.create(user=instance, role='admin')
        else:
            # Regular users default to the 'user' role.
            Profile.objects.create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    """
    Ensures the Profile is saved whenever the User is saved.
    """
    # This handles updates to existing users.
    if hasattr(instance, 'profile'):
        instance.profile.save()
