from django.db import models
from apps.training.models import TrainingJob
import uuid

class TrainingResult(models.Model):
    """
    Represents a single result file from a training job.
    """
    job = models.ForeignKey(TrainingJob, on_delete=models.CASCADE, related_name='results')
    identifier = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    file_path = models.CharField(max_length=1024)
    file_size = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Result {self.identifier} for job {self.job.identifier}"
