"""
Forms for the users application.
Defines the forms used for authentication, user creation, profile updates,
and password management, with custom styling for the Tailwind CSS UI.
"""

from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
    UserCreationForm,
    UsernameField,
)
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _

from users.models import ROLE_CHOICES, Profile


# Standard CSS classes for consistent styling across all authentication forms.
# These classes target the custom Tailwind CSS dashboard theme.
AUTH_INPUT_CLASSES = (
    "bg-gray-50 border border-gray-300 text-gray-900 sm:text-sm rounded-lg "
    "focus:ring-purple-700 focus:border-purple-700 block w-full p-2.5 "
    "dark:bg-gray-700 dark:border-gray-600 dark:placeholder-gray-400 "
    "dark:text-white dark:focus:ring-purple-300 dark:focus:border-purple-300"
)


class SigninForm(AuthenticationForm):
    """Form used for user login (Sign In)."""

    username = UsernameField(
        widget=forms.TextInput(
            attrs={
                "autofocus": True,
                "class": AUTH_INPUT_CLASSES,
                "placeholder": "name@company.com",
            }
        )
    )
    password = forms.CharField(
        label=_("Password"),
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "autocomplete": "current-password",
                "class": AUTH_INPUT_CLASSES,
                "placeholder": "••••••••",
            }
        ),
    )


class SignupForm(UserCreationForm):
    """Form used for registering new users (Sign Up)."""

    # Custom field to select the user's role during registration.
    role = forms.ChoiceField(
        choices=ROLE_CHOICES,
        required=True,
        widget=forms.Select(attrs={"class": AUTH_INPUT_CLASSES}),
    )

    # GDPR Compliance: Contractual agreement and Privacy acknowledgment.
    # Note: We distinguish between 'agreement' to terms (contract) and
    # 'acknowledgment' of the privacy policy (information/transparency).
    accept_terms = forms.BooleanField(
        required=True,
        label=_("I agree to the Terms and Conditions"),
        help_text=_("Necessary for the performance of our contract with you."),
        widget=forms.CheckboxInput(
            attrs={
                "class": "w-4 h-4 border-gray-300 rounded bg-gray-50 focus:ring-3 focus:ring-purple-700 dark:focus:ring-purple-600 dark:ring-offset-gray-800 dark:bg-gray-700 dark:border-gray-600"
            }
        ),
    )

    accept_privacy = forms.BooleanField(
        required=True,
        label=_("I have read and acknowledge the Privacy Policy"),
        help_text=_(
            "Explains how we process your data based on contract, legal obligation, and legitimate interests."
        ),
        widget=forms.CheckboxInput(
            attrs={
                "class": "w-4 h-4 border-gray-300 rounded bg-gray-50 focus:ring-3 focus:ring-purple-700 dark:focus:ring-purple-600 dark:ring-offset-gray-800 dark:bg-gray-700 dark:border-gray-600"
            }
        ),
    )

    class Meta:
        """Meta configuration for SignupForm."""

        model = User
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
        )

    def __init__(self, *args, **kwargs):
        """
        Custom initialization to apply consistent styling and placeholders
        to all fields automatically.
        """
        super().__init__(*args, **kwargs)

        for field_name, field in self.fields.items():
            # Skip custom styling for checkboxes to preserve their appearance.
            if isinstance(field.widget, forms.CheckboxInput):
                continue

            # Set placeholder to the field's label if not already set.
            if not field.widget.attrs.get("placeholder"):
                field.widget.attrs["placeholder"] = field.label

            # Apply the standard CSS classes.
            field.widget.attrs["class"] = AUTH_INPUT_CLASSES
            # Ensure required fields are marked as such in the HTML.
            field.widget.attrs["required"] = True

    def save(self, commit=True):
        """
        Extends the standard save method to record GDPR consent
        in the associated Profile model.
        """
        user = super().save(commit=commit)
        if commit:
            # Update the profile with consent information.
            from django.utils import timezone

            profile = user.profile
            profile.accepted_policy = self.cleaned_data.get("accept_privacy")
            profile.accepted_policy_date = timezone.now()
            profile.accepted_terms = self.cleaned_data.get("accept_terms")
            profile.accepted_terms_date = timezone.now()
            profile.save()
        return user


class AdminAddUserForm(UserCreationForm):
    """Form used by admins to add new users without requiring legal agreement at creation."""

    role = forms.ChoiceField(
        choices=ROLE_CHOICES,
        required=True,
        widget=forms.Select(attrs={"class": AUTH_INPUT_CLASSES}),
    )

    class Meta:
        """Meta configuration for AdminAddUserForm."""

        model = User
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if not field.widget.attrs.get("placeholder"):
                field.widget.attrs["placeholder"] = field.label
            field.widget.attrs["class"] = AUTH_INPUT_CLASSES
            field.widget.attrs["required"] = True


class UserUpdateForm(forms.ModelForm):
    """Form used by admins to update basic user information."""

    role = forms.ChoiceField(choices=ROLE_CHOICES, required=True)

    class Meta:
        """Meta configuration for UserUpdateForm."""

        model = User
        fields = ("username", "first_name", "last_name", "email")


class UserPasswordResetForm(PasswordResetForm):
    """Form used to request a password reset email."""

    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={"class": AUTH_INPUT_CLASSES, "placeholder": "name@company.com"}
        )
    )


class UserSetPasswordForm(SetPasswordForm):
    """Form used to set a new password after a reset request."""

    new_password1 = forms.CharField(
        max_length=50,
        label="New Password",
        widget=forms.PasswordInput(
            attrs={"class": AUTH_INPUT_CLASSES, "placeholder": "New Password"}
        ),
    )
    new_password2 = forms.CharField(
        max_length=50,
        label="Confirm New Password",
        widget=forms.PasswordInput(
            attrs={"class": AUTH_INPUT_CLASSES, "placeholder": "Confirm New Password"}
        ),
    )


class UserPasswordChangeForm(PasswordChangeForm):
    """Form used by users to change their password while logged in."""

    old_password = forms.CharField(
        max_length=50,
        label="Old Password",
        widget=forms.PasswordInput(
            attrs={"class": AUTH_INPUT_CLASSES, "placeholder": "Old Password"}
        ),
    )
    new_password1 = forms.CharField(
        max_length=50,
        label="New Password",
        widget=forms.PasswordInput(
            attrs={"class": AUTH_INPUT_CLASSES, "placeholder": "New Password"}
        ),
    )
    new_password2 = forms.CharField(
        max_length=50,
        label="Confirm New Password",
        widget=forms.PasswordInput(
            attrs={"class": AUTH_INPUT_CLASSES, "placeholder": "Confirm New Password"}
        ),
    )


class ProfileForm(forms.ModelForm):
    """Form used by users to update their own profile information."""

    class Meta:
        """Meta configuration for ProfileForm."""

        model = Profile
        # 'user' and 'role' are not directly editable by the user in this form.
        exclude = ("user", "role")

    def __init__(self, *args, **kwargs):
        """Standardize widget attributes for the profile form."""
        super().__init__(*args, **kwargs)

        for field_name, field in self.fields.items():
            field.widget.attrs["placeholder"] = field.label
            # Profile form uses slightly different classes (shadow-sm).
            field.widget.attrs["class"] = (
                "shadow-sm bg-gray-50 border border-gray-300 text-gray-900 "
                "sm:text-sm rounded-lg focus:ring-purple-700 focus:border-purple-700 "
                "block w-full p-2.5 dark:bg-gray-700 dark:border-gray-600 "
                "dark:placeholder-gray-400 dark:text-white dark:focus:ring-purple-300 "
                "dark:focus:border-purple-300"
            )
            field.widget.attrs["required"] = False
