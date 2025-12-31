"""
Admin configuration for the users application.
Registers the Profile model with the Django admin interface.
"""

from django.contrib import admin

from .models import Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    """
    Configuration for the Profile model in the admin panel.
    """
    # Display the username and role in the list view.
    list_display = ('user', 'role', 'identifier')

    # Enable filtering by role.
    list_filter = ('role',)

    # Enable searching by username and full name.
    search_fields = ('user__username', 'full_name', 'identifier')

    # Mark unique identifiers as read-only.
    readonly_fields = ('identifier',)
