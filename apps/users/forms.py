"""Forms for the users application.
Defines the forms used for authentication, user creation, profile updates,
and password management, with custom styling for the Tailwind CSS UI.
"""

from django import forms
from django.contrib.auth.forms import (
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
    UserCreationForm,
)
from django.contrib.auth.models import User

from users.models import ROLE_CHOICES, Profile

# Standard CSS classes for consistent styling across all authentication forms.
# These classes target the custom Tailwind CSS dashboard theme.
AUTH_INPUT_CLASSES = (
    "bg-gray-50 border border-gray-300 text-gray-900 sm:text-sm rounded-lg "
    "focus:ring-purple-700 focus:border-purple-700 block w-full p-2.5 "
    "dark:bg-gray-700 dark:border-gray-600 dark:placeholder-gray-400 "
    "dark:text-white dark:focus:ring-purple-300 dark:focus:border-purple-300"
)


class AdminAddUserForm(UserCreationForm):
    """Form used by admins to add new users without requiring legal agreement at creation.

    Attributes:
        role (ChoiceField): The role to assign to the new user.
    """

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
        """Initializes the form and sets custom styling for all fields.

        Args:
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.
        """
        super().__init__(*args, **kwargs)
        for _field_name, field in self.fields.items():
            if not field.widget.attrs.get("placeholder"):
                field.widget.attrs["placeholder"] = field.label
            field.widget.attrs["class"] = AUTH_INPUT_CLASSES
            field.widget.attrs["required"] = True


class UserUpdateForm(forms.ModelForm):
    """Form used by admins to update basic user information.

    Attributes:
        role (ChoiceField): The role assigned to the user.
    """

    role = forms.ChoiceField(
        choices=ROLE_CHOICES,
        required=True,
        widget=forms.Select(attrs={"class": AUTH_INPUT_CLASSES}),
    )

    class Meta:
        """Meta configuration for UserUpdateForm."""

        model = User
        fields = ("username", "first_name", "last_name", "email")

    def __init__(self, *args, **kwargs):
        """Initializes the form and sets custom styling for most fields.

        Args:
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.
        """
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if field_name != "role":
                field.widget.attrs["class"] = AUTH_INPUT_CLASSES


class UserPasswordResetForm(PasswordResetForm):
    """Form used to request a password reset email."""

    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "class": AUTH_INPUT_CLASSES,
                "placeholder": "name@company.com",
            }
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
            attrs={
                "class": AUTH_INPUT_CLASSES,
                "placeholder": "Confirm New Password",
            }
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
            attrs={
                "class": AUTH_INPUT_CLASSES,
                "placeholder": "Confirm New Password",
            }
        ),
    )


class ProfileForm(forms.ModelForm):
    """Form used by users to update their own profile information."""

    class Meta:
        """Meta configuration for ProfileForm."""

        model = Profile
        # 'user' and 'role' are not directly editable by the user in this form.
        # Compliance, restriction, and emergency fields are managed by the system or administrators.
        exclude = (
            "user",
            "role",
            "color",
            "is_restricted",
            "restriction_date",
            "accepted_policy",
            "accepted_policy_date",
            "accepted_terms",
            "accepted_terms_date",
            "cookie_consent",
            "cookie_consent_date",
            "is_emergency_access",
            "emergency_access_expiry",
            "emergency_access_justification",
        )

    def __init__(self, *args, **kwargs):
        """Standardize widget attributes for the profile form.

        Args:
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.
        """
        super().__init__(*args, **kwargs)

        for _field_name, field in self.fields.items():
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
