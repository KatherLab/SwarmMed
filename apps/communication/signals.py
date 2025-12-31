from django.db.models.signals import post_save
from django.dispatch import receiver
from django.core.mail import send_mail
from django.conf import settings
from .models import Message, ProjectPost

@receiver(post_save, sender=Message)
def send_message_notification(sender, instance, created, **kwargs):
    """
    Sends an email notification to the recipient when a new message is created.
    """
    if created and instance.recipient.email:
        subject = f"New Message from {instance.sender.username}"
        body = (
            f"Hello {instance.recipient.username},\n\n"
            f"You have received a new message from {instance.sender.username}.\n\n"
            f"Subject: {instance.subject}\n\n"
            "Please log in to your MediSwarmCloud account to view the full message."
        )
        
        # Use EMAIL_HOST_USER or fallback to None (which uses DEFAULT_FROM_EMAIL)
        from_email = getattr(settings, 'EMAIL_HOST_USER', None)

        try:
            send_mail(
                subject,
                body,
                from_email,
                [instance.recipient.email],
                fail_silently=True,
            )
        except Exception:
            # Fail silently if email configuration is invalid or service is down
            pass

@receiver(post_save, sender=ProjectPost)
def send_project_post_notification(sender, instance, created, **kwargs):
    """
    Sends email notifications to project members when a new post is added to the board.
    """
    if created:
        project = instance.project
        author = instance.author
        
        # Collect all recipients: Project author + Project members
        recipients = set()
        if project.author.email:
            recipients.add(project.author)
        
        for member in project.members.all():
            if member.email:
                recipients.add(member)
        
        # Remove the author of the post from recipients if present
        if author in recipients:
            recipients.remove(author)
            
        if not recipients:
            return

        subject = f"New Post on Project Board: {project.title}"
        body_template = (
            "Hello,\n\n"
            f"{author.username} has posted a new update on the project board for '{project.title}'.\n\n"
            f"Content snippet:\n{instance.content[:200]}...\n\n"
            "Please log in to MediSwarmCloud to view the full post and reply."
        )
        
        from_email = getattr(settings, 'EMAIL_HOST_USER', None)

        # Send individual emails to respect privacy and avoid huge To lists
        for recipient in recipients:
            try:
                send_mail(
                    subject,
                    body_template,
                    from_email,
                    [recipient.email],
                    fail_silently=True,
                )
            except Exception:
                pass

