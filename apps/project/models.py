"""
Database models for the project application.
Defines the structure for projects, file storage paths, and current project tracking.
"""

import os
import secrets

from common.fields import EncryptedCharField
from common.models import AbstractBaseModel
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import models
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

# Import local utility functions for generating dynamic file paths
from .utils import (
    data_validation_path,
    data_visualization_path,
    requirements_path,
    results_visualization_path,
    training_code_path,
)


class Project(AbstractBaseModel):
    """
    The central Project model that stores information about collaborative
    learning tasks, including various code scripts and requirement files.
    """

    # Possible states of a project:
    # 'IN_PROGRESS' is the initial state, 'ARCHIVED' indicates completion.
    STATUS_CHOICES = [
        ("IN_PROGRESS", "In Progress"),
        ("ARCHIVED", "Archived"),
    ]

    # Core project metadata
    # title: Human-readable name of the project
    title = models.CharField(max_length=255)

    # author: The User who created the project.
    # If the user is deleted, all their projects are deleted (CASCADE).
    author = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="created_projects"
    )

    # members: Other users who can view or participate in this project.
    # This is a Many-to-Many relationship, allowing multiple users per project.
    members = models.ManyToManyField(
        User, related_name="member_projects", blank=True
    )

    # Optional detailed description of the project's purpose.
    description = models.TextField(blank=True)

    # Current status of the project, defaulting to 'In Progress'.
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="IN_PROGRESS",
        db_index=True,
    )

    # File fields for different types of scripts required by the platform.
    # Each field uses a specific utility function to determine where to store
    # the file.

    # Python code that performs the actual training of the model.
    training_code = models.FileField(
        upload_to=training_code_path, max_length=512, blank=True, null=True
    )

    # requirements.txt file listing the dependencies for the training code.
    requirements_file = models.FileField(
        upload_to=requirements_path, max_length=512, blank=True, null=True
    )

    # Script for validating the format and quality of user-provided data.
    data_validation_script = models.FileField(
        upload_to=data_validation_path, max_length=512, blank=True, null=True
    )

    # Script for visualizing the training dataset.
    data_visualization_script = models.FileField(
        upload_to=data_visualization_path,
        max_length=512,
        blank=True,
        null=True,
    )

    # Script for visualizing the final training results (e.g., loss curves).
    results_visualization_script = models.FileField(
        upload_to=results_visualization_path,
        max_length=512,
        blank=True,
        null=True,
    )

    # UI helper to mark a project as globally active (deprecated in favor of
    # UserCurrentProject).
    is_current = models.BooleanField(default=False)

    # Secure secret for training containers to access project-specific APIs
    secret = EncryptedCharField(
        max_length=128,
        blank=True,
        null=True,
        unique=True,
        help_text="Project-specific secret for container authentication.",
    )

    def __str__(self):
        """Returns the project title as its string representation."""
        return self.title

    def save(self, *args, **kwargs):
        """
        Custom save method to handle automatic cleanup of old files
        and generation of project secrets.
        """
        # Auto-generate secret if not set
        if not self.secret:
            self.secret = secrets.token_urlsafe(48)

        from .tasks import cleanup_project_files

        # If self.pk exists, it means we are updating an existing project.
        if self.pk:
            try:
                # Fetch the version of the project currently saved in the
                # database
                old_instance = Project.objects.get(pk=self.pk)

                def trigger_cleanup(old_file, new_file, subfolder):
                    """
                    Helper function to detect file changes and trigger
                    background deletion of the old folder.
                    """
                    # Check if an old file exists and is being replaced by a
                    # different file.
                    if (
                        old_file
                        and new_file
                        and str(old_file) != str(new_file)
                    ):
                        # Construct the relative directory path for this
                        # specific script type.
                        folder_path = os.path.join(
                            str(self.identifier), subfolder
                        )

                        # Trigger background task for deletion
                        cleanup_project_files.delay(folder_path)

                # Check each file field and trigger cleanup if it has changed.
                trigger_cleanup(
                    old_instance.training_code,
                    self.training_code,
                    "code/training/",
                )
                trigger_cleanup(
                    old_instance.requirements_file,
                    self.requirements_file,
                    "code/requirements/",
                )
                trigger_cleanup(
                    old_instance.data_validation_script,
                    self.data_validation_script,
                    "code/data_validation/",
                )
                trigger_cleanup(
                    old_instance.data_visualization_script,
                    self.data_visualization_script,
                    "code/data_visualization/",
                )
                trigger_cleanup(
                    old_instance.results_visualization_script,
                    self.results_visualization_script,
                    "code/results_visualization/",
                )

            except Project.DoesNotExist:
                # If the instance doesn't exist yet, it's a brand new project.
                pass

        # Call the parent class's save method to actually save the project to
        # the database.
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """
        Custom delete method to ensure all files associated with
        the project are removed from storage when the database record is deleted.
        """
        from .tasks import delete_all_project_files

        # Trigger background task for full cleanup
        delete_all_project_files.delay(str(self.identifier))

        # Call the standard Django delete operation to remove the database
        # record.
        super().delete(*args, **kwargs)


class UserCurrentProject(models.Model):
    """
    Links a user to the project they are currently viewing or interacting with.
    This acts as a global 'active project' context for the user interface.
    """

    # Each user can have at most one 'current project' at any given time.
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="current_project_relation"
    )

    # The project they are currently working on. Can be null if no project is
    # active.
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        related_name="current_for_users",
    )

    class Meta:
        """Extra configuration for the UserCurrentProject model."""

        # Ensure a specific user-project pair only appears once (redundant due
        # to OneToOneField).
        unique_together = ("user", "project")

    def __str__(self):
        """Returns string representation of the relation for the admin interface."""
        project_title = self.project.title if self.project else "None"
        return f"{self.user.username}'s active project: {project_title}"


# Cache invalidation signals
@receiver([post_save, post_delete], sender=Project)
def invalidate_project_cache(sender, instance, **kwargs):
    """Invalidate project list cache when projects are created, updated, or deleted."""
    # Clear cache for project author
    cache.delete(f"project_list_{instance.author.id}")
    # Clear cache for all members
    for member in instance.members.all():
        cache.delete(f"project_list_{member.id}")


@receiver(m2m_changed, sender=Project.members.through)
def invalidate_project_cache_on_member_change(
    sender, instance, action, **kwargs
):
    """Invalidate cache when project members are added or removed."""
    if action in ["post_add", "post_remove", "post_clear"]:
        # Clear cache for project author
        cache.delete(f"project_list_{instance.author.id}")
        # Clear cache for all current members
        for member in instance.members.all():
            cache.delete(f"project_list_{member.id}")


@receiver([post_save, post_delete], sender=UserCurrentProject)
def invalidate_project_list_on_current_change(sender, instance, **kwargs):
    """Invalidate project list cache when a user's current project changes."""
    cache.delete(f"project_list_{instance.user.id}")
