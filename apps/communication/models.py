from django.db import models
from django.contrib.auth.models import User
from apps.project.models import Project

class Message(models.Model):
    sender = models.ForeignKey(User, related_name='sent_messages', on_delete=models.CASCADE)
    recipient = models.ForeignKey(User, related_name='received_messages', on_delete=models.CASCADE)
    subject = models.CharField(max_length=255)
    body = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"From {self.sender} to {self.recipient}: {self.subject}"

class ProjectPost(models.Model):
    project = models.ForeignKey(Project, related_name='posts', on_delete=models.CASCADE)
    author = models.ForeignKey(User, related_name='project_posts', on_delete=models.CASCADE)
    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"Post by {self.author} on {self.project}"

class ProjectBoardAccess(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='board_access')
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='access_logs')
    last_accessed = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'project')