"""
URL configuration for the users application.
Maps web addresses to view functions for authentication, profile management,
and administrative user control.
"""

from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic.base import RedirectView
# from two_factor.urls import urlpatterns as tf_urls

from . import views
from . import two_factor_views
from common import views as common_views

# Application namespace for user-related URLs.
app_name = "users"

two_factor_patterns = [
    path('login/', two_factor_views.LoginView.as_view(), name='login'),
    path('two_factor/setup/', two_factor_views.SetupView.as_view(), name='setup'),
    path('two_factor/qrcode/', two_factor_views.QRGeneratorView.as_view(), name='qr'),
    path('two_factor/setup/complete/', two_factor_views.SetupCompleteView.as_view(), name='setup_complete'),
    path('two_factor/backup/tokens/', two_factor_views.BackupTokensView.as_view(), name='backup_tokens'),
    path('two_factor/', two_factor_views.ProfileView.as_view(), name='profile'),
    path('two_factor/disable/', two_factor_views.DisableView.as_view(), name='disable'),
]

urlpatterns = [
    # Basic index view.
    path("", views.index, name="index"),
    # --- Authentication Endpoints ---
    # Multi-Factor Authentication
    # path("auth/", include(tf_urls)),
    path("auth/", include((two_factor_patterns, 'two_factor'), namespace='two_factor')),
    path("auth/signin/",
        RedirectView.as_view(pattern_name="users:two_factor:login", permanent=False, query_string=True),
        name="signin",
    ),
    path("auth/signout/", auth_views.LogoutView.as_view(), name="signout"),
    # --- Password Reset Flow ---
    # Step 1: Request reset email.
    path(
        "auth/password-reset/", views.UserPasswordResetView.as_view(), name="password_reset"
    ),
    # Step 2: Confirmation message that email was sent.
    path(
        "auth/password-reset-done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="apps/users/auth/password-reset-done.html"
        ),
        name="password_reset_done",
    ),
    # Step 3: Click link in email and enter new password.
    path(
        "auth/password-reset-confirm/<uidb64>/<token>/",
        views.UserPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    # Step 4: Success message after password is reset.
    path(
        "auth/password-reset-complete/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="apps/users/auth/password-reset-complete.html"
        ),
        name="password_reset_complete",
    ),
    # --- User Profile & Settings ---
    path("settings/", views.settings, name="settings"),
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
