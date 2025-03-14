from django.db import models

class MedicalData(models.Model):
    file = models.FileField(upload_to='medical_data/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"MedicalData {self.id}"
