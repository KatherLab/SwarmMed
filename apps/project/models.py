from django.db import models
from django.contrib.auth.models import User
import uuid
import os
import shutil
from django.conf import settings
from django.core.files.storage import default_storage
from .utils import training_code_path, data_validation_path, data_visualization_path, results_visualization_path

class Project(models.Model):
    """
    Project model to store information about projects including code files.
    """
    STATUS_CHOICES = [
        ('IN_PROGRESS', 'In Progress'),
        ('FINISHED', 'Finished'),
    ]
    title = models.CharField(max_length=255)
    identifier = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_projects')
    members = models.ManyToManyField(User, related_name='member_projects', blank=True)
    creation_date = models.DateField(auto_now_add=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='IN_PROGRESS')
    
    # File fields
    training_code = models.FileField(upload_to=training_code_path, blank=True, null=True)
    data_validation_script = models.FileField(upload_to=data_validation_path, blank=True, null=True)
    data_visualization_script = models.FileField(upload_to=data_visualization_path, blank=True, null=True)
    results_visualization_script = models.FileField(upload_to=results_visualization_path, blank=True, null=True)
    
    is_current = models.BooleanField(default=False)

    def __str__(self):
        return self.title
    
    def save(self, *args, **kwargs):
        """Override save to handle file replacements"""
        if self.pk:
            try:
                old_instance = Project.objects.get(pk=self.pk)
                
                # Helper function to handle file replacement
                def replace_file_and_cleanup(old_file, new_file, subfolder):
                    if old_file and new_file and str(old_file) != str(new_file):
                        # Delete all files in the directory
                        folder_path = os.path.join(str(self.identifier), subfolder)
                        if hasattr(default_storage, 'bucket'):  # For S3
                            prefix = folder_path
                            s3_objects = default_storage.bucket.objects.filter(Prefix=prefix)
                            s3_objects.delete()
                        else:  # For local storage
                            full_path = os.path.join(settings.MEDIA_ROOT, folder_path)
                            if os.path.exists(full_path):
                                shutil.rmtree(full_path)
                                os.makedirs(full_path, exist_ok=True)
                        
                        # Delete the reference to the old file
                        old_file.delete(save=False)
                
                # Handle file replacements for each field
                replace_file_and_cleanup(old_instance.training_code, self.training_code, 'code/training/')
                replace_file_and_cleanup(old_instance.data_validation_script, self.data_validation_script, 'code/data_validation/')
                replace_file_and_cleanup(old_instance.data_visualization_script, self.data_visualization_script, 'code/data_visualization/')
                replace_file_and_cleanup(old_instance.results_visualization_script, self.results_visualization_script, 'code/results_visualization/')
            
            except Project.DoesNotExist:
                pass  # New instance
                
        super(Project, self).save(*args, **kwargs)
    
    def delete(self, *args, **kwargs):
        """Override delete method to delete all associated files and directories"""
        
        # For S3, delete all objects with the project identifier prefix
        if hasattr(default_storage, 'bucket'):  # Check if using S3
            prefix = str(self.identifier) + '/'
            s3_objects = default_storage.bucket.objects.filter(Prefix=prefix)
            s3_objects.delete()
            
        # Call the parent delete method
        super(Project, self).delete(*args, **kwargs)

class UserCurrentProject(models.Model):
    """Model to track which project is currently active for a user"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='current_project_relation')
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, related_name='current_for_users')
    
    class Meta:
        unique_together = ('user', 'project')

