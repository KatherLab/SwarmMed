"""
URL configuration for the users application.
Maps web addresses to view functions for authentication, profile management,
and administrative user control.
"""

from django.contrib.auth import views as auth_views
from django.urls import path

from . import views
from common import views as common_views

# Application namespace for user-related URLs.
app_name = "users"

urlpatterns = [
    # Basic index view.
    path("", views.index, name="index"),
    # --- Authentication Endpoints ---
    path("signin/", views.RedirectToTwoFactorLogin.as_view(), name="signin"),
    path("signup/", views.SignUpView.as_view(), name="signup"),
    path("signout/", views.signout_view, name="signout"),
    # --- Password Reset Flow ---
    # Step 1: Request reset email.
    path(
        "password-reset/", views.UserPasswordResetView.as_view(), name="password_reset"
    ),
    # Step 2: Confirmation message that email was sent.
    path(
        "password-reset-done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="apps/users/auth/password-reset-done.html"
        ),
        name="password_reset_done",
    ),
    # Step 3: Click link in email and enter new password.
    path(
        "password-reset-confirm/<uidb64>/<token>/",
        views.UserPasswrodResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    # Step 4: Success message after password is reset.
    path(
        "password-reset-complete/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="apps/users/auth/password-reset-complete.html"
        ),
        name="password_reset_complete",
    ),
    # --- User Profile & Settings ---
    path("profile/", views.profile, name="profile"),
    path("change-password/", views.change_password, name="change_password"),
    path("delete-account/", views.delete_own_account, name="delete_account"),
    path("export-data/", views.export_user_data, name="export_data"),
    path(
        "update-cookie-consent/",
        views.update_cookie_consent,
        name="update_cookie_consent",
    ),
    # --- Legal & Privacy ---
    path("privacy-policy/", common_views.privacy_policy, name="privacy_policy"),
    path(
        "terms-and-conditions/",
        common_views.terms_and_conditions,
        name="terms_and_conditions",
    ),
    path("accept-terms/", views.accept_terms, name="accept_terms"),
    # --- Admin User Management ---
    # List and search all users.
    path("user-list/", views.user_list, name="user_list"),
    # Perform actions on specific users.
    path("delete-user/<int:id>/", views.delete_user, name="delete_user"),
    path("update-user/<int:id>/", views.update_user, name="update_user"),
    path(
        "user-change-password/<int:id>/",
        views.user_change_password,
        name="user_change_password",
    ),
    path(
        "toggle-emergency-access/<int:id>/",
        views.toggle_emergency_access,
        name="toggle_emergency_access",
    ),
]
