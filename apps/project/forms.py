"""Forms for the project application.
This module defines the forms used for creating and updating projects,
including custom logic for handling project member identifiers.
"""

from django import forms

from users.models import Profile

from .models import Project
from .utils import training_code_upload_error


class ProjectForm(forms.ModelForm):
    """Form for creating and editing a Project instance.
    Includes an additional field for entering member UUIDs as strings.
    """

    # A text area field for users to paste multiple UUIDs separated by commas or new lines.
    # This is not a model field, so we define it explicitly.
    member_identifiers = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "placeholder": "Enter UUIDs separated by commas or new lines",
            }
        ),
        help_text="Enter the UUID identifiers of users to add as members",
    )

    class Meta:
        """Metadata for the ProjectForm, linking it to the Project model."""

        model = Project
        # List of fields from the Project model to include in the form.
        # Note: 'member_identifiers' is added manually above.
        fields = [
            "title",
            "description",
            "training_code",
            "requirements_file",
            "data_validation_script",
            "data_visualization_script",
            "results_visualization_script",
            "explainability_script",
        ]

    def __init__(self, *args, **kwargs):
        """Initialize the form.
        If editing an existing project, we prepopulate the member_identifiers field.
        """
        # Extract the project instance if provided (standard for editing
        # existing objects).
        instance = kwargs.get("instance")
        super().__init__(*args, **kwargs)

        # If we are editing an existing project (instance exists),
        # gather the UUID identifiers of its current members to display them in
        # the form.
        if instance:
            # We look up each member's Profile to get their unique UUID identifier.
            # This list comprehension creates a list of UUID strings.
            current_member_uuids = []
            for member in instance.members.all():
                try:
                    profile = Profile.objects.get(user=member)
                    current_member_uuids.append(str(profile.identifier))
                except Profile.DoesNotExist:
                    # If a user somehow doesn't have a profile, skip them.
                    continue

            # Join the UUIDs into a single comma-separated string for the text
            # area field.
            self.initial["member_identifiers"] = ", ".join(
                current_member_uuids
            )

    def clean(self):
        """Refuse training code without a root-level training.py.

        Training runs the training.py at the root of the uploaded code, so an
        upload of Python files without one is shown as an error on the
        training code field instead of being stored.
        """
        cleaned_data = super().clean()
        error = training_code_upload_error(self.data, self.files)
        if error:
            self.add_error("training_code", error)
        return cleaned_data
