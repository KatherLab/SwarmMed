"""
Admin configuration for the users application.
Registers the Profile model with the Django admin interface and integrates
it into the standard User admin for a unified view.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from .models import Profile


class ProfileInline(admin.StackedInline):
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


class UserAdmin(BaseUserAdmin):
    """
    Extended User admin that includes the Profile inline.
    """

    inlines = (ProfileInline,)
    list_display = BaseUserAdmin.list_display + (
        "get_role",
        "get_emergency_access",
        "date_joined",
        "last_login",
    )
    list_filter = BaseUserAdmin.list_filter + (
        "profile__role",
        "profile__is_emergency_access",
    )
    readonly_fields = ("date_joined", "last_login")
    date_hierarchy = "date_joined"

    def get_role(self, obj):
        return obj.profile.role

    get_role.short_description = "Role"

    def get_emergency_access(self, obj):
        return obj.profile.is_emergency_access

    get_emergency_access.short_description = "Emergency"
    get_emergency_access.boolean = True


# Re-register User admin
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    """
    Configuration for the Profile model in the admin panel.
    """

    # Display more relevant fields in the list view.
    list_display = (
        "user",
        "role",
        "is_emergency_access",
        "country",
        "city",
        "cookie_consent",
        "identifier",
    )

    # Enable filtering by role, emergency access, and cookie consent.
    list_filter = ("role", "is_emergency_access", "cookie_consent", "country")

    # Enable searching by username, email, full name and identifiers.
    search_fields = (
        "user__username",
        "user__email",
        "full_name",
        "identifier",
        "phone",
        "city",
    )

    # Logical groupings for the detail view.
    fieldsets = (
        ("Basic Information", {"fields": ("user", "identifier", "role", "full_name")}),
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
