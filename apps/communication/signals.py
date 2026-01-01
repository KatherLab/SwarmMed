"""
Signal handlers for the communication app.
These functions are automatically triggered by specific database events,
such as sending an email notification when a new message is saved.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.core.mail import send_mail
from django.conf import settings
from .models import Message, ProjectPost
from apps.logs.logger import get_logger

logger = get_logger()


@receiver(post_save, sender=Message)
def send_message_notification(sender, instance, created, **kwargs):
    """
    Sends an email notification to the recipient when a new direct message is created.
    """
    # Only send notification if the message was just created (not updated)
    # and the recipient has an email address associated with their account.
    if created and instance.recipient.email:
        subject = f"New Message from {instance.sender.username}"
        body = (
            f"Hello {instance.recipient.username},\n\n"
            f"You have received a new message from {instance.sender.username}.\n\n"
            f"Subject: {instance.subject}\n\n"
            "Please log in to your SwarmCloud account to view the full message.")

        # Use EMAIL_HOST_USER from settings or fallback to None
        # (Django will then use DEFAULT_FROM_EMAIL).
        from_email = getattr(settings, 'EMAIL_HOST_USER', None)

        try:
            # Attempt to send the email
            send_mail(
                subject,
                body,
                from_email,
                [instance.recipient.email],
                fail_silently=True,
            )
        except Exception as e:
            # If email sending fails (e.g., bad config), we log it
            # to prevent the application from crashing.
            logger.project.error(f"Failed to send message notification email: {e}")


@receiver(post_save, sender=ProjectPost)
def send_project_post_notification(sender, instance, created, **kwargs):
    """
    Sends email notifications to project members when a new post is added to the board.
    """
    if created:
        project = instance.project
        author = instance.author

        # Collect all recipients: the project creator and all invited members
        recipients = set()

        # Add project author to recipients if they have an email
        if project.author.email:
            recipients.add(project.author)

        # Add all members who have an email
        for member in project.members.all():
            if member.email:
                recipients.add(member)

        # We don't want to send an email to the person who just wrote the post
        if author in recipients:
            recipients.remove(author)

        # If there are no recipients to notify, we stop here
        if not recipients:
            return

        subject = f"New Post on Project Board: {project.title}"
        body_template = (
            "Hello,\n\n"
            f"{author.username} has posted a new update on the project board "
            f"for '{project.title}'.\n\n"
            f"Content snippet:\n{instance.content[:200]}...\n\n"
            "Please log in to SwarmCloud to view the full post and reply."
        )

        from_email = getattr(settings, 'EMAIL_HOST_USER', None)

        # Send individual emails to each recipient to maintain privacy
        # (prevents users from seeing each other's email addresses in a group list).
        for recipient in recipients:
            try:
                send_mail(
                    subject,
                    body_template,
                    from_email,
                    [recipient.email],
                    fail_silently=True,
                )
            except Exception as e:
                # Catch and log errors during email delivery
                logger.project.error(f"Failed to send project post notification email to {recipient.email}: {e}")
