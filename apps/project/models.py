from django.db import models
from django.contrib.auth.models import User
import uuid
import os
import shutil
from django.conf import settings

def get_upload_path(instance, filename, subfolder):
    """Generic function to get upload path based on project UUID and subfolder"""
    return os.path.join(str(instance.identifier), subfolder, filename)

def training_code_path(instance, filename):
    return get_upload_path(instance, filename, 'code/training')

def data_validation_path(instance, filename):
    return get_upload_path(instance, filename, 'code/data_validation')

def results_visualization_path(instance, filename):
    return get_upload_path(instance, filename, 'code/results_visualization')

class Project(models.Model):
    title = models.CharField(max_length=255)
    identifier = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_projects')
    members = models.ManyToManyField(User, related_name='member_projects', blank=True)
    creation_date = models.DateField()
    description = models.TextField(blank=True)
    
    # Updated file fields to use UUID-based paths
    training_code = models.FileField(upload_to=training_code_path, blank=True, null=True)
    data_validation_script = models.FileField(upload_to=data_validation_path, blank=True, null=True)
    results_visualization_script = models.FileField(upload_to=results_visualization_path, blank=True, null=True)
    
    is_current = models.BooleanField(default=False)

    def __str__(self):
        return self.title
    
    def save(self, *args, **kwargs):
        # Check if this is an existing instance (has ID)
        if self.pk:
            # Get the old instance from the database
            old_instance = Project.objects.get(pk=self.pk)
            
            # Check if training_code file has changed
            if old_instance.training_code and self.training_code != old_instance.training_code:
                # Delete the old file
                old_instance.training_code.delete(save=False)
                
            # Check if data_validation_script file has changed
            if old_instance.data_validation_script and self.data_validation_script != old_instance.data_validation_script:
                # Delete the old file
                old_instance.data_validation_script.delete(save=False)
                
            # Check if results_visualization_script file has changed
            if old_instance.results_visualization_script and self.results_visualization_script != old_instance.results_visualization_script:
                # Delete the old file
                old_instance.results_visualization_script.delete(save=False)
                
        # Call the parent save method to save the new file
        super(Project, self).save(*args, **kwargs)
    
    def delete(self, *args, **kwargs):
        """Override delete method to delete all associated files and directories"""
        # Delete individual files first
        if self.training_code:
            self.training_code.delete(save=False)
        if self.data_validation_script:
            self.data_validation_script.delete(save=False)
        if self.results_visualization_script:
            self.results_visualization_script.delete(save=False)
        
        # Delete the entire project directory if it exists
        project_dir = os.path.join(settings.MEDIA_ROOT, str(self.identifier))
        if os.path.exists(project_dir):
            shutil.rmtree(project_dir)
            
        # Call the parent delete method
        super(Project, self).delete(*args, **kwargs)

class UserCurrentProject(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='current_project_relation')
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, related_name='current_for_users')
    
    class Meta:
        unique_together = ('user', 'project')
