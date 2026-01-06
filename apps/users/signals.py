"""
Signal handlers for the users application.
Listens for database events related to the User model to automatically
manage associated Profile instances.
"""

from django.contrib.auth.models import Group, User
from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver
from logs.logger import get_logger

from .models import ROLE_CHOICES, Profile


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """
    Automatically creates a Profile instance whenever a new User is created.
    """
    if created:
        if instance.is_superuser:
            # Superusers are automatically granted the 'admin' role.
            Profile.objects.create(user=instance, role="admin")
        else:
            # Regular users default to the 'user' role.
            Profile.objects.create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    """
    Ensures the Profile is saved whenever the User is saved.
    """
    # This handles updates to existing users.
    try:
        if hasattr(instance, "profile"):
            instance.profile.save()
    except Profile.DoesNotExist:
        # If for some reason the profile doesn't exist, we don't want to crash.
        # It should have been created by create_user_profile for new users.
        pass


@receiver(post_save, sender=Profile)
def sync_role_to_group(sender, instance, **kwargs):
    """
    Synchronizes the Profile.role with Django's built-in Group system.
    This ensures roles are visible and manageable via the standard Django admin.
    """
    user = instance.user
    role = instance.role

    # Define all possible role names from the choices keys.
    all_roles = [choice[0] for choice in ROLE_CHOICES]

    for role_name in all_roles:
        # We ensure the Group exists in the database.
        group, _ = Group.objects.get_or_create(name=role_name)

        if role_name == role:
            # Add the user to the group corresponding to their current role.
            if not user.groups.filter(name=role_name).exists():
                user.groups.add(group)
        else:
            # Remove the user from other groups that represent different roles.
            if user.groups.filter(name=role_name).exists():
                user.groups.remove(group)


@receiver(m2m_changed, sender=User.groups.through)
def sync_group_to_role(sender, instance, action, reverse, pk_set, **kwargs):
    """
    Synchronizes Django Groups back to Profile.role when groups are modified.
    This allows managing roles via the standard User admin Groups section.
    """
    if action in ["post_add", "post_remove", "post_clear"] and not reverse:
        # Avoid recursion: sync_role_to_group also modifies groups.
        # We only update if the role actually needs to change.
        user = instance
        if not hasattr(user, "profile"):
            return

        groups = user.groups.values_list("name", flat=True)

        # Priority mapping: admin > developer > user
        new_role = None
        if "admin" in groups:
            new_role = "admin"
        elif "developer" in groups:
            new_role = "developer"
        elif "user" in groups:
            new_role = "user"

        if new_role and user.profile.role != new_role:
            # Disconnect the signal temporarily to avoid infinite loop
            post_save.disconnect(sync_role_to_group, sender=Profile)
            user.profile.role = new_role
            user.profile.save()
            post_save.connect(sync_role_to_group, sender=Profile)


@receiver(user_logged_in)
def log_user_login(sender, request, user, **kwargs):
    """
    Logs successful user login events.
    """
    logger = get_logger(user=user)
    ip_address = request.META.get("REMOTE_ADDR")
    logger.auth.info(
        f"User logged in from {ip_address}", ip_address=ip_address
    )


@receiver(user_logged_out)
def log_user_logout(sender, request, user, **kwargs):
    """
    Logs user logout events.
    """
    logger = get_logger(user=user)
    logger.auth.info("User logged out")


@receiver(user_login_failed)
def log_login_failed(sender, credentials, request, **kwargs):
    """
    Logs failed login attempts.
    """
    username = credentials.get("username", "unknown")
    ip_address = request.META.get("REMOTE_ADDR") if request else "unknown"

    # We don't have a user object, so we log with user=None.
    logger = get_logger()
    logger.auth.warning(
        f"Failed login attempt for username '{username}' from {ip_address}",
        username=username,
        ip_address=ip_address,
    )
