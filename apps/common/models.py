"""
Shared abstract models for the entire project.
"""

import uuid
from django.db import models


class AbstractBaseModel(models.Model):
    """
    An abstract base class that provides a UUID identifier and
    standard creation/update timestamps for all models.
    """

    identifier = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        help_text="Unique identifier for this object across the system.",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="The date and time this object was created.",
    )

    updated_at = models.DateTimeField(
        auto_now=True, help_text="The date and time this object was last updated."
    )

    class Meta:
        abstract = True
