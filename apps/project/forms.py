from django import forms
from django.contrib.auth.models import User
from .models import Project

class ProjectForm(forms.ModelForm):
    members = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'form-control'})
    )
    
    class Meta:
        model = Project
        fields = [
            'title', 'creation_date', 'description',
            'training_code', 'data_validation_script', 
            'results_visualization_script', 'members'
        ]
        widgets = {
            'creation_date': forms.DateInput(attrs={'type': 'date'}),
        }
        
    def __init__(self, *args, **kwargs):
        super(ProjectForm, self).__init__(*args, **kwargs)
        # Display users by email rather than username
        self.fields['members'].label_from_instance = lambda obj: obj.email
