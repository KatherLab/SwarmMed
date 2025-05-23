from django import forms
from .models import Project

class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = [
            'title', 'author', 'creation_date', 'description',
            'training_code', 'data_validation_script', 'results_visualization_script'
        ]
        widgets = {
            'creation_date': forms.DateInput(attrs={'type': 'date'}),
        }