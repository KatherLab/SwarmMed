"""Database models for the users application.
Defines the Profile model which extends the standard Django User model
with additional fields like role and contact information.
"""

import secrets

from django.contrib.auth.models import User
from django.db import models

from common.fields import EncryptedCharField, EncryptedTextField
from common.models import AbstractBaseModel

# Define available roles for users in the system.
# 'admin' has full control, 'developer' can manage projects,
# and 'user' is a standard participant.
ROLE_CHOICES = (
    ("admin", "Admin"),
    ("developer", "Developer"),
    ("user", "User"),
)

# A predefined palette of Tailwind-compatible colors for user avatars.
AVATAR_COLORS = [
    "#F87171",  # red-400
    "#FB923C",  # orange-400
    "#FBBF24",  # amber-400
    "#FACC15",  # yellow-400
    "#A3E635",  # lime-400
    "#4ADE80",  # green-400
    "#34D399",  # emerald-400
    "#2DD4BF",  # teal-400
    "#22D3EE",  # cyan-400
    "#38BDF8",  # sky-400
    "#60A5FA",  # blue-400
    "#818CF8",  # indigo-400
    "#A78BFA",  # violet-400
    "#C084FC",  # purple-400
    "#E879F9",  # fuchsia-400
    "#FB7185",  # pink-400
]


class Profile(AbstractBaseModel):
    """Extends the built-in Django User model with extra application-specific information.

    Attributes:
        user (OneToOneField): Link to the standard Django User.
        role (CharField): The user's role in the system (admin, developer, user).
        color (CharField): HEX color code for the user's avatar background.
        full_name (EncryptedCharField): The user's full name (Encrypted).
        country (EncryptedCharField): The user's country (Encrypted).
        city (EncryptedCharField): The user's city (Encrypted).
        zip_code (EncryptedCharField): The user's ZIP code (Encrypted).
        address (EncryptedCharField): The user's address (Encrypted).
        phone (EncryptedCharField): The user's phone number (Encrypted).
        accepted_policy (BooleanField): Whether the user accepted the privacy policy.
        accepted_policy_date (DateTimeField): When the privacy policy was accepted.
        accepted_terms (BooleanField): Whether the user accepted the terms of service.
        accepted_terms_date (DateTimeField): When the terms of service were accepted.
        cookie_consent (CharField): The user's cookie consent status.
        cookie_consent_date (DateTimeField): When the cookie consent was given.
        is_restricted (BooleanField): Whether processing of user data is restricted (GDPR).
        restriction_date (DateTimeField): When the restriction was applied.
        is_emergency_access (BooleanField): Whether the user has emergency access (HIPAA).
        emergency_access_expiry (DateTimeField): When the emergency access expires.
        emergency_access_justification (EncryptedTextField): Justification for emergency access.
    """

    # Link to the standard Django User.
    # If the User is deleted, the Profile is also deleted (CASCADE).
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    # The user's role in the MedSwarmHub system.
    role = models.CharField(
        max_length=20, choices=ROLE_CHOICES, default="user"
    )

    # HEX color code for the user's avatar background.
    color = models.CharField(max_length=7, null=True, blank=True)

    # Optional contact and location information (Encrypted PHI).
    full_name = EncryptedCharField(max_length=255, null=True, blank=True)
    country = EncryptedCharField(max_length=255, null=True, blank=True)
    city = EncryptedCharField(max_length=255, null=True, blank=True)
    zip_code = EncryptedCharField(max_length=255, null=True, blank=True)
    address = EncryptedCharField(max_length=255, null=True, blank=True)
    phone = EncryptedCharField(max_length=255, null=True, blank=True)

    # Compliance: Track user consent to legal documents and cookies.
    accepted_policy = models.BooleanField(default=False)
    accepted_policy_date = models.DateTimeField(null=True, blank=True)
    accepted_terms = models.BooleanField(default=False)
    accepted_terms_date = models.DateTimeField(null=True, blank=True)
    cookie_consent = models.CharField(
        max_length=20,
        choices=[("accepted", "Accepted"), ("rejected", "Rejected")],
        null=True,
        blank=True,
    )
    cookie_consent_date = models.DateTimeField(null=True, blank=True)

    # GDPR: Right to Restriction of Processing
    is_restricted = models.BooleanField(default=False)
    restriction_date = models.DateTimeField(null=True, blank=True)

    # Break-glass / Emergency Access (HIPAA compliance)
    is_emergency_access = models.BooleanField(default=False)
    emergency_access_expiry = models.DateTimeField(null=True, blank=True)
    emergency_access_justification = EncryptedTextField(null=True, blank=True)

    def __str__(self):
        """Returns the username of the associated user.

        Returns:
            str: The username of the user.
        """
        return self.user.username

    def get_avatar_color(self):
        """Retrieves or picks a random avatar color.

        Saves the choice to ensure persistence if none exists.

        Returns:
            str: The HEX color code for the avatar.
        """
        if not self.color:
            self.color = secrets.choice(AVATAR_COLORS)
            self.save(update_fields=["color"])
        return self.color
