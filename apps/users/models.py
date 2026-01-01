"""
Database models for the users application.
Defines the Profile model which extends the standard Django User model
with additional fields like role and contact information.
"""

import uuid
import secrets

from django.contrib.auth.models import User
from django.db import models


# Define available roles for users in the system.
# 'admin' has full control, 'developer' can manage projects,
# and 'user' is a standard participant.
ROLE_CHOICES = (
    ('admin', 'Admin'),
    ('developer', 'Developer'),
    ('user', 'User'),
)

# A predefined palette of Tailwind-compatible colors for user avatars.
AVATAR_COLORS = [
    '#F87171',  # red-400
    '#FB923C',  # orange-400
    '#FBBF24',  # amber-400
    '#FACC15',  # yellow-400
    '#A3E635',  # lime-400
    '#4ADE80',  # green-400
    '#34D399',  # emerald-400
    '#2DD4BF',  # teal-400
    '#22D3EE',  # cyan-400
    '#38BDF8',  # sky-400
    '#60A5FA',  # blue-400
    '#818CF8',  # indigo-400
    '#A78BFA',  # violet-400
    '#C084FC',  # purple-400
    '#E879F9',  # fuchsia-400
    '#FB7185',  # pink-400
]


class Profile(models.Model):
    """
    Extends the built-in Django User model with extra application-specific
    information using a One-to-One relationship.
    """

    # Link to the standard Django User.
    # If the User is deleted, the Profile is also deleted (CASCADE).
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    # A unique UUID identifier for the user profile,
    # useful for secure public identification.
    identifier = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True
    )

    # The user's role in the SwarmCloud system.
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default='user'
    )

    # HEX color code for the user's avatar background.
    color = models.CharField(max_length=7, null=True, blank=True)

    # Optional contact and location information.
    full_name = models.CharField(max_length=255, null=True, blank=True)
    country = models.CharField(max_length=255, null=True, blank=True)
    city = models.CharField(max_length=255, null=True, blank=True)
    zip_code = models.CharField(max_length=255, null=True, blank=True)
    address = models.CharField(max_length=255, null=True, blank=True)
    phone = models.CharField(max_length=255, null=True, blank=True)

    def __str__(self):
        """Returns the username of the associated user."""
        return self.user.username

    def get_avatar_color(self):
        """
        Retrieves the assigned avatar color or picks a random one if none exists.
        Saves the choice to ensure persistence.
        """
        if not self.color:
            self.color = secrets.choice(AVATAR_COLORS)
            self.save(update_fields=['color'])
        return self.color
