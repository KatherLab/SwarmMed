from django.db import models
import uuid

class Project(models.Model):
    title = models.CharField(max_length=255)
    identifier = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    author = models.CharField(max_length=255)
    creation_date = models.DateField()
    description = models.TextField(blank=True)
    training_code = models.FileField(upload_to='training_code/', blank=True, null=True)
    data_validation_script = models.FileField(upload_to='data_validation/', blank=True, null=True)
    results_visualization_script = models.FileField(upload_to='results_visualization/', blank=True, null=True)
    is_current = models.BooleanField(default=False)

    def __str__(self):
        return self.title