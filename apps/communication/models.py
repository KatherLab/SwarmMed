"""
Models for the communication app.
This file defines the database structure for direct messages,
project board posts, and tracking board access.
"""

from common.fields import EncryptedCharField, EncryptedTextField
from common.models import AbstractBaseModel
from django.contrib.auth.models import User
from django.db import models
from project.models import Project


class Message(AbstractBaseModel):
    """
    Represents a direct message sent between two users.
    """

    # The user who sent the message
    sender = models.ForeignKey(
        User, related_name="sent_messages", on_delete=models.CASCADE
    )
    # The user who receives the message
    recipient = models.ForeignKey(
        User, related_name="received_messages", on_delete=models.CASCADE
    )
    # The subject line of the message (Encrypted)
    subject = EncryptedCharField(max_length=255)
    # The actual content of the message (Encrypted)
    body = EncryptedTextField()
    # Tracks if the recipient has seen the message
    is_read = models.BooleanField(default=False, db_index=True)

    class Meta:
        # Most recent messages appear first
        ordering = ["-created_at"]
        indexes = [
            # Optimize fetching conversation history between two users
            models.Index(fields=["sender", "recipient", "-created_at"]),
            models.Index(fields=["recipient", "sender", "-created_at"]),
            # Optimize counting unread messages for a recipient
            models.Index(fields=["recipient", "is_read", "-created_at"]),
        ]

    def __str__(self):
        """String representation of the message object."""
        return f"From {self.sender} to {self.recipient}: {self.subject}"


class ProjectPost(AbstractBaseModel):
    """
    Represents a post or update made on a specific project's board.
    """

    # The project this post belongs to
    project = models.ForeignKey(
        Project, related_name="posts", on_delete=models.CASCADE
    )
    # The user who wrote the post
    author = models.ForeignKey(
        User, related_name="project_posts", on_delete=models.CASCADE
    )
    # The content of the update (Encrypted)
    content = EncryptedTextField()

    class Meta:
        # Most recent posts appear first
        ordering = ["-created_at"]
        indexes = [
            # Optimize fetching posts for a project board
            models.Index(fields=["project", "-created_at"]),
        ]

    def __str__(self):
        """String representation of the project post."""
        return f"Post by {self.author} on {self.project}"


class ProjectBoardAccess(AbstractBaseModel):
    """
    Tracks when a user last viewed a project's board.
    This is used to identify 'unread' or 'new' posts for that user.
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="board_access"
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="access_logs"
    )

    class Meta:
        # Ensure a unique log entry for each user-project pair
        unique_together = ("user", "project")
        indexes = [
            # Optimize lookups for checking last access time
            models.Index(fields=["project", "updated_at"]),
            models.Index(fields=["user", "project"]),
        ]
