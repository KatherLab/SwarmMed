"""Database models for the communication application.

Defines the structures for direct messages, project board posts,
and tracking board access for users.
"""

from django.contrib.auth.models import User
from django.db import models

from common.fields import EncryptedCharField, EncryptedTextField
from common.models import AbstractBaseModel
from project.models import Project


class Message(AbstractBaseModel):
    """Represents a direct message sent between two users.

    Messages are encrypted for privacy and support read tracking.

    Attributes:
        sender (ForeignKey): The user who sent the message.
        recipient (ForeignKey): The user who receives the message.
        subject (EncryptedCharField): The subject line of the message (encrypted).
        body (EncryptedTextField): The actual content of the message (encrypted).
        is_read (BooleanField): Tracks if the recipient has seen the message.
    """

    sender = models.ForeignKey(
        User,
        related_name="sent_messages",
        on_delete=models.CASCADE,
        help_text="The user who sent the message.",
    )
    recipient = models.ForeignKey(
        User,
        related_name="received_messages",
        on_delete=models.CASCADE,
        help_text="The user who receives the message.",
    )
    subject = EncryptedCharField(
        max_length=255, help_text="The subject line of the message (encrypted)."
    )
    body = EncryptedTextField(
        help_text="The actual content of the message (encrypted)."
    )
    is_read = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Tracks if the recipient has seen the message.",
    )

    class Meta:
        """Meta options for the Message model."""

        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["sender", "recipient", "-created_at"]),
            models.Index(fields=["recipient", "sender", "-created_at"]),
            models.Index(fields=["recipient", "is_read", "-created_at"]),
        ]

    def __str__(self):
        """Returns a string representation of the message.

        Returns:
            str: A string indicating the sender, recipient, and subject.
        """
        return f"From {self.sender} to {self.recipient}: {self.subject}"


class ProjectPost(AbstractBaseModel):
    """Represents a post or update made on a specific project's board.

    Posts allow users to share updates and notes within a project.

    Attributes:
        project (ForeignKey): The project this post belongs to.
        author (ForeignKey): The user who wrote the post.
        content (EncryptedTextField): The content of the update (encrypted).
    """

    project = models.ForeignKey(
        Project,
        related_name="posts",
        on_delete=models.CASCADE,
        help_text="The project this post belongs to.",
    )
    author = models.ForeignKey(
        User,
        related_name="project_posts",
        on_delete=models.CASCADE,
        help_text="The user who wrote the post.",
    )
    content = EncryptedTextField(help_text="The content of the update (encrypted).")

    class Meta:
        """Meta options for the ProjectPost model."""

        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project", "-created_at"]),
        ]

    def __str__(self):
        """Returns a string representation of the project post.

        Returns:
            str: A string indicating the author and the project.
        """
        return f"Post by {self.author} on {self.project}"


class ProjectBoardAccess(AbstractBaseModel):
    """Tracks when a user last viewed a project's board.

    This is used to identify 'unread' or 'new' posts for that user.

    Attributes:
        user (ForeignKey): The user who accessed the board.
        project (ForeignKey): The project board that was accessed.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="board_access",
        help_text="The user who accessed the board.",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="access_logs",
        help_text="The project board that was accessed.",
    )

    class Meta:
        """Meta options for the ProjectBoardAccess model."""

        unique_together = ("user", "project")
        indexes = [
            models.Index(fields=["project", "updated_at"]),
            models.Index(fields=["user", "project"]),
        ]

    def __str__(self):
        """Returns a string representation of the board access entry.

        Returns:
            str: A string indicating which user accessed which project board.
        """
        return f"{self.user} accessed {self.project} board"
