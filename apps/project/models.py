from django.db import models
from django.contrib.auth.models import User
import uuid

class Project(models.Model):
    title = models.CharField(max_length=255)
    identifier = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_projects')
    members = models.ManyToManyField(User, related_name='member_projects', blank=True)
    creation_date = models.DateField()
    description = models.TextField(blank=True)
    training_code = models.FileField(upload_to='training_code/', blank=True, null=True)
    data_validation_script = models.FileField(upload_to='data_validation/', blank=True, null=True)
    results_visualization_script = models.FileField(upload_to='results_visualization/', blank=True, null=True)
    
    # This will be replaced with a user-specific implementation
    is_current = models.BooleanField(default=False)

    def __str__(self):
        return self.title

class UserCurrentProject(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='current_project_relation')
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, related_name='current_for_users')
    
    class Meta:
        unique_together = ('user', 'project')