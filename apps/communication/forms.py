from django import forms
from django.contrib.auth.models import User
from .models import Message, ProjectPost
from apps.project.models import Project

class MessageForm(forms.ModelForm):
    recipient = forms.ModelChoiceField(
        queryset=User.objects.all(),
        widget=forms.Select(attrs={
            'class': 'bg-gray-50 border border-gray-300 text-gray-900 text-sm rounded-lg focus:ring-blue-500 focus:border-blue-500 block w-full p-2.5 dark:bg-gray-700 dark:border-gray-600 dark:placeholder-gray-400 dark:text-white dark:focus:ring-blue-500 dark:focus:border-blue-500'
        })
    )
    
    class Meta:
        model = Message
        fields = ['recipient', 'subject', 'body']
        widgets = {
            'subject': forms.TextInput(attrs={
                'class': 'bg-gray-50 border border-gray-300 text-gray-900 text-sm rounded-lg focus:ring-blue-500 focus:border-blue-500 block w-full p-2.5 dark:bg-gray-700 dark:border-gray-600 dark:placeholder-gray-400 dark:text-white dark:focus:ring-blue-500 dark:focus:border-blue-500', 
                'placeholder': 'Subject'
            }),
            'body': forms.Textarea(attrs={
                'class': 'block p-2.5 w-full text-sm text-gray-900 bg-gray-50 rounded-lg border border-gray-300 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-700 dark:border-gray-600 dark:placeholder-gray-400 dark:text-white dark:focus:ring-blue-500 dark:focus:border-blue-500', 
                'placeholder': 'Type your message...', 
                'rows': 5
            }),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        project = kwargs.pop('project', None)
        super(MessageForm, self).__init__(*args, **kwargs)
        
        # If a project is provided, filter recipients to members + author
        if user and project:
             # Get members and author
             potential_recipients = set(project.members.all())
             potential_recipients.add(project.author)
             
             # Remove self from recipients
             if user in potential_recipients:
                 potential_recipients.remove(user)
             
             self.fields['recipient'].queryset = User.objects.filter(id__in=[u.id for u in potential_recipients])

class ProjectPostForm(forms.ModelForm):
    class Meta:
        model = ProjectPost
        fields = ['content']
        widgets = {
            'content': forms.Textarea(attrs={
                'class': 'block p-2.5 w-full text-sm text-gray-900 bg-gray-50 rounded-lg border border-gray-300 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-700 dark:border-gray-600 dark:placeholder-gray-400 dark:text-white dark:focus:ring-blue-500 dark:focus:border-blue-500', 
                'placeholder': 'Write an update or question...', 
                'rows': 3
            }),
        }
