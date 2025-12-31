"""
Models for the communication app.
This file defines the database structure for direct messages,
project board posts, and tracking board access.
"""

from django.db import models
from django.contrib.auth.models import User
from apps.project.models import Project


class Message(models.Model):
    """
    Represents a direct message sent between two users.
    """
    # The user who sent the message
    sender = models.ForeignKey(
        User,
        related_name='sent_messages',
        on_delete=models.CASCADE
    )
    # The user who receives the message
    recipient = models.ForeignKey(
        User,
        related_name='received_messages',
        on_delete=models.CASCADE
    )
    # The subject line of the message
    subject = models.CharField(max_length=255)
    # The actual content of the message
    body = models.TextField()
    # When the message was sent (automatically set on creation)
    timestamp = models.DateTimeField(auto_now_add=True)
    # Tracks if the recipient has seen the message
    is_read = models.BooleanField(default=False)

    class Meta:
        # Most recent messages appear first
        ordering = ['-timestamp']

    def __str__(self):
        """String representation of the message object."""
        return f"From {self.sender} to {self.recipient}: {self.subject}"


class ProjectPost(models.Model):
    """
    Represents a post or update made on a specific project's board.
    """
    # The project this post belongs to
    project = models.ForeignKey(
        Project,
        related_name='posts',
        on_delete=models.CASCADE
    )
    # The user who wrote the post
    author = models.ForeignKey(
        User,
        related_name='project_posts',
        on_delete=models.CASCADE
    )
    # The content of the update
    content = models.TextField()
    # When the post was made
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Most recent posts appear first
        ordering = ['-timestamp']

    def __str__(self):
        """String representation of the project post."""
        return f"Post by {self.author} on {self.project}"


class ProjectBoardAccess(models.Model):
    """
    Tracks when a user last viewed a project's board.
    This is used to identify 'unread' or 'new' posts for that user.
    """
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='board_access'
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='access_logs'
    )
    # Automatically updated every time the record is saved
    last_accessed = models.DateTimeField(auto_now=True)

    class Meta:
        # Ensure a unique log entry for each user-project pair
        unique_together = ('user', 'project')
