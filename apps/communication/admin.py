"""
Admin configuration for the communication app.
This file registers the models with the Django admin interface,
allowing administrators to manage Messages and ProjectPosts.
"""

from django.contrib import admin
from .models import Message, ProjectPost


# Register the Message model so it's accessible in the admin panel.
# This allows administrators to view, add, or delete messages directly.
admin.site.register(Message)

# Register the ProjectPost model so it's accessible in the admin panel.
# This allows administrators to manage posts made on project boards.
admin.site.register(ProjectPost)
