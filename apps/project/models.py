"""
Database models for the project application.
Defines the structure for projects, file storage paths, and current project tracking.
"""

import os
import shutil
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.db import models

# Import local utility functions for generating dynamic file paths
from .utils import (
    data_validation_path,
    data_visualization_path,
    requirements_path,
    results_visualization_path,
    training_code_path,
)


class Project(models.Model):
    """
    The central Project model that stores information about collaborative
    learning tasks, including various code scripts and requirement files.
    """

    # Possible states of a project:
    # 'IN_PROGRESS' is the initial state, 'ARCHIVED' indicates completion.
    STATUS_CHOICES = [
        ('IN_PROGRESS', 'In Progress'),
        ('ARCHIVED', 'Archived'),
    ]

    # Core project metadata
    # title: Human-readable name of the project
    title = models.CharField(max_length=255)

    # identifier: A unique UUID used to identify the project in the system.
    # It's also used to create unique folders in storage (S3 or local).
    identifier = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True
    )

    # author: The User who created the project.
    # If the user is deleted, all their projects are deleted (CASCADE).
    author = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='created_projects'
    )

    # members: Other users who can view or participate in this project.
    # This is a Many-to-Many relationship, allowing multiple users per project.
    members = models.ManyToManyField(
        User,
        related_name='member_projects',
        blank=True
    )

    # Automatically set to the current date when the project is first created.
    creation_date = models.DateField(auto_now_add=True)

    # Optional detailed description of the project's purpose.
    description = models.TextField(blank=True)

    # Current status of the project, defaulting to 'In Progress'.
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='IN_PROGRESS'
    )

    # File fields for different types of scripts required by the platform.
    # Each field uses a specific utility function to determine where to store
    # the file.

    # Python code that performs the actual training of the model.
    training_code = models.FileField(
        upload_to=training_code_path,
        blank=True,
        null=True
    )

    # requirements.txt file listing the dependencies for the training code.
    requirements_file = models.FileField(
        upload_to=requirements_path,
        blank=True,
        null=True
    )

    # Script for validating the format and quality of user-provided data.
    data_validation_script = models.FileField(
        upload_to=data_validation_path,
        blank=True,
        null=True
    )

    # Script for visualizing the training dataset.
    data_visualization_script = models.FileField(
        upload_to=data_visualization_path,
        blank=True,
        null=True
    )

    # Script for visualizing the final training results (e.g., loss curves).
    results_visualization_script = models.FileField(
        upload_to=results_visualization_path,
        blank=True,
        null=True
    )

    # UI helper to mark a project as globally active (deprecated in favor of
    # UserCurrentProject).
    is_current = models.BooleanField(default=False)

    def __str__(self):
        """Returns the project title as its string representation."""
        return self.title

    def save(self, *args, **kwargs):
        """
        Custom save method to handle automatic cleanup of old files.
        If a user replaces an old file with a new one, we delete the old file
        from the storage to prevent orphan files taking up space.
        """
        # If self.pk exists, it means we are updating an existing project.
        if self.pk:
            try:
                # Fetch the version of the project currently saved in the
                # database
                old_instance = Project.objects.get(pk=self.pk)

                def replace_file_and_cleanup(old_file, new_file, subfolder):
                    """
                    Helper function to detect file changes and delete the
                    entire subfolder of the old file to avoid clutter.
                    """
                    # Check if an old file exists and is being replaced by a
                    # different file.
                    if old_file and new_file and str(
                            old_file) != str(new_file):
                        # Construct the relative directory path for this
                        # specific script type.
                        folder_path = os.path.join(
                            str(self.identifier), subfolder)

                        # Handle deletion differently depending on where files
                        # are stored.
                        if hasattr(default_storage, 'bucket'):
                            # Using Amazon S3 storage (via django-storages).
                            prefix = folder_path
                            s3_objects = default_storage.bucket.objects.filter(
                                Prefix=prefix
                            )
                            s3_objects.delete()
                        else:
                            # Using standard local filesystem storage.
                            full_path = os.path.join(
                                settings.MEDIA_ROOT, folder_path)
                            if os.path.exists(full_path):
                                # Delete the entire folder and its contents.
                                shutil.rmtree(full_path)
                                # Re-create the folder so it's ready for the
                                # new file.
                                os.makedirs(full_path, exist_ok=True)

                        # Explicitly remove the old file's reference from the
                        # storage backend.
                        old_file.delete(save=False)

                # Check each file field and trigger cleanup if it has changed.
                replace_file_and_cleanup(
                    old_instance.training_code,
                    self.training_code,
                    'code/training/'
                )
                replace_file_and_cleanup(
                    old_instance.requirements_file,
                    self.requirements_file,
                    'code/requirements/'
                )
                replace_file_and_cleanup(
                    old_instance.data_validation_script,
                    self.data_validation_script,
                    'code/data_validation/'
                )
                replace_file_and_cleanup(
                    old_instance.data_visualization_script,
                    self.data_visualization_script,
                    'code/data_visualization/'
                )
                replace_file_and_cleanup(
                    old_instance.results_visualization_script,
                    self.results_visualization_script,
                    'code/results_visualization/'
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
        # If using S3 storage, delete all objects that start with this
        # project's UUID.
        if hasattr(default_storage, 'bucket'):
            prefix = f"{str(self.identifier)}/"
            s3_objects = default_storage.bucket.objects.filter(Prefix=prefix)
            s3_objects.delete()

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
        User,
        on_delete=models.CASCADE,
        related_name='current_project_relation'
    )

    # The project they are currently working on. Can be null if no project is
    # active.
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        related_name='current_for_users'
    )

    class Meta:
        """Extra configuration for the UserCurrentProject model."""
        # Ensure a specific user-project pair only appears once (redundant due
        # to OneToOneField).
        unique_together = ('user', 'project')

    def __str__(self):
        """Returns string representation of the relation for the admin interface."""
        project_title = self.project.title if self.project else "None"
        return f"{self.user.username}'s active project: {project_title}"
