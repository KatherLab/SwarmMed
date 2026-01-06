"""
Admin configuration for the users application.
Registers the Profile model with the Django admin interface and integrates
it into the standard User admin for a unified view.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, StackedInline
from unfold.forms import (
    AdminPasswordChangeForm,
    UserChangeForm,
    UserCreationForm,
)

from .models import Profile


class ProfileInline(StackedInline):
    """
    Allows editing the Profile directly within the User admin page.
    """

    model = Profile
    can_delete = False
    verbose_name_plural = "Profile Information"
    fk_name = "user"
    readonly_fields = (
        "identifier",
        "accepted_terms_date",
        "accepted_policy_date",
        "cookie_consent_date",
    )


class UserAdmin(BaseUserAdmin, ModelAdmin):
    """
    Extended User admin that includes the Profile inline.
    """

    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm
    inlines = (ProfileInline,)
    list_display = (
        "display_header",
        "is_active",
        "get_role",
        "get_emergency_access",
        "date_joined",
    )
    list_filter = (
        "is_active",
        "is_staff",
        "profile__role",
        "profile__is_emergency_access",
    )
    readonly_fields = ("date_joined", "last_login")
    date_hierarchy = "date_joined"

    def display_header(self, instance):
        return instance.username

    display_header.short_description = _("User")

    def get_role(self, obj):
        from unfold.decorators import display

        @display(
            label={
                "ADMIN": "danger",
                "RESEARCHER": "info",
                "PROVIDER": "success",
            }
        )
        def role_label(instance):
            return instance.profile.role

        return role_label(obj)

    get_role.short_description = "Role"

    def get_emergency_access(self, obj):
        from unfold.decorators import display

        @display(boolean=True, label=True)
        def emergency_label(instance):
            return instance.profile.is_emergency_access

        return emergency_label(obj)

    get_emergency_access.short_description = "Emergency"


# Re-register User admin
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(Profile)
class ProfileAdmin(ModelAdmin):
    """
    Configuration for the Profile model in the admin panel.
    """

    # Display more relevant fields in the list view.
    list_display = (
        "user",
        "role_label",
        "emergency_label",
        "country",
        "city",
        "identifier",
    )

    # Enable filtering by role, emergency access, and cookie consent.
    list_filter = (
        "role",
        "is_emergency_access",
        "cookie_consent",
        "country",
    )

    # Enable searching by username, email, full name and identifiers.
    search_fields = (
        "user__username",
        "user__email",
        "full_name",
        "identifier",
    )

    def role_label(self, obj):
        from unfold.decorators import display

        @display(
            label={
                "ADMIN": "danger",
                "RESEARCHER": "info",
                "PROVIDER": "success",
            }
        )
        def label(instance):
            return instance.role

        return label(obj)

    role_label.short_description = "Role"

    def emergency_label(self, obj):
        from unfold.decorators import display

        @display(boolean=True, label=True)
        def label(instance):
            return instance.is_emergency_access

        return label(obj)

    emergency_label.short_description = "Emergency"

    # Logical groupings for the detail view.
    fieldsets = (
        (
            "Basic Information",
            {"fields": ("user", "identifier", "role", "full_name")},
        ),
        (
            "Contact & Location",
            {"fields": ("phone", "address", "zip_code", "city", "country")},
        ),
        (
            "Compliance & Security",
            {
                "fields": (
                    "is_emergency_access",
                    "emergency_access_expiry",
                    "emergency_access_justification",
                )
            },
        ),
        (
            "Legal Consents",
            {
                "fields": (
                    "accepted_terms",
                    "accepted_terms_date",
                    "accepted_policy",
                    "accepted_policy_date",
                    "cookie_consent",
                    "cookie_consent_date",
                )
            },
        ),
    )

    # Mark sensitive and auto-generated fields as read-only.
    readonly_fields = (
        "identifier",
        "accepted_terms_date",
        "accepted_policy_date",
        "cookie_consent_date",
    )
