"""Shared abstract models for the entire project.

This module provides base model classes that include common fields like
UUID identifiers and timestamps, ensuring consistency across all
database models in the application.
"""

import uuid

from django.db import models


class AbstractBaseModel(models.Model):
    """An abstract base class for all models in the project.

    Provides a UUID identifier and standard creation/update timestamps.

    Attributes:
        identifier (UUID): Unique identifier for this object.
        created_at (DateTimeField): The date and time this object was created.
        updated_at (DateTimeField): The date and time this object was last updated.
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
        auto_now=True,
        help_text="The date and time this object was last updated.",
    )

    class Meta:
        """Meta options for AbstractBaseModel."""
        abstract = True
