from django import forms
from django.contrib.auth.models import User
from .models import Project
from ..users.models import Profile 


class ProjectForm(forms.ModelForm):
    member_identifiers = forms.CharField(
        required=False, 
        widget=forms.Textarea(attrs={'class': 'form-control', 'placeholder': 'Enter UUIDs separated by commas or new lines'}),
        help_text="Enter the UUID identifiers of users to add as members"
    )
    
    class Meta:
        model = Project
        fields = [
            'title', 'creation_date', 'description',
            'training_code', 'data_validation_script', 'data_visualization_script',
            'results_visualization_script'
        ]
        # Note: 'members' field is removed from here
        widgets = {
            'creation_date': forms.DateInput(attrs={'type': 'date'}),
        }
    
    def __init__(self, *args, **kwargs):
        instance = kwargs.get('instance', None)
        super(ProjectForm, self).__init__(*args, **kwargs)
        
        # If editing an existing project, populate the member_identifiers field
        if instance:
            member_identifiers = [str(Profile.objects.get(user=member).identifier) 
                                for member in instance.members.all()]
            self.initial['member_identifiers'] = ', '.join(member_identifiers)

