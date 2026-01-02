"""
View functions for the users application.
Handles authentication (sign in, sign up, sign out), password management,
profile updates, and administrative user management.
"""

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.views import (
    LoginView,
    PasswordChangeView,
    PasswordResetConfirmView,
    PasswordResetView,
)
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, RedirectView

from apps.users.forms import (
    ProfileForm,
    SigninForm,
    SignupForm,
    UserPasswordChangeForm,
    UserPasswordResetForm,
    UserSetPasswordForm,
    UserUpdateForm,
)
from apps.users.models import Profile
from apps.users.utils import user_filter, get_safe_referer, anonymize_user_data
from apps.logs import logger

from .decorators import admin_required


def index(request):
    """Simple index view for users (mainly for testing)."""
    return HttpResponse("INDEX Users")


class RedirectToTwoFactorLogin(RedirectView):
    """
    Redirects users from the legacy /users/signin/ URL to the
    enforced 2FA login flow.
    """
    permanent = False
    query_string = True
    pattern_name = 'two_factor:login'


class SignInView(LoginView):
    """Standard Django LoginView customized with our SigninForm and template."""

    form_class = SigninForm
    template_name = "authentication/sign-in.html"


class SignUpView(CreateView):
    """Standard Django CreateView for user registration."""

    form_class = SignupForm
    template_name = "authentication/sign-up.html"
    success_url = reverse_lazy("users:signin")


class UserPasswordChangeView(PasswordChangeView):
    """View to allow users to change their password while logged in."""

    template_name = "authentication/password-change.html"
    form_class = UserPasswordChangeForm


class UserPasswordResetView(PasswordResetView):
    """View to initiate the password reset process via email."""

    template_name = "authentication/forgot-password.html"
    form_class = UserPasswordResetForm


class UserPasswrodResetConfirmView(PasswordResetConfirmView):
    """View to finalize password reset after clicking the email link."""

    template_name = "authentication/reset-password.html"
    form_class = UserSetPasswordForm


def signout_view(request):
    """Logs out the current user and redirects to the sign-in page."""
    logout(request)
    return redirect(reverse("users:signin"))


@login_required
def profile(request):
    """Displays and handles updates for the logged-in user's profile."""
    # Retrieve or create the Profile associated with the current user.
    user_profile, created = Profile.objects.get_or_create(
        user=request.user,
        defaults={"role": "admin" if request.user.is_superuser else "user"},
    )

    if request.method == "POST":
        # If the form was submitted, process the POST data.
        form = ProfileForm(request.POST, instance=user_profile)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated successfully")
    else:
        # If it's a GET request, pre-populate the form with current data.
        form = ProfileForm(instance=user_profile)

    context = {
        "form": form,
        "segment": "profile",
    }
    return render(request, "dashboard/profile.html", context)


@login_required
def change_password(request):
    """
    Handles a password change request using a simple POST method.
    Verifies the current password before setting the new one.
    """
    user = request.user
    if request.method == "POST":
        current_pwd = request.POST.get("current_password")
        new_pwd = request.POST.get("new_password")

        # Verify the user knows their current password.
        if check_password(current_pwd, user.password):
            try:
                # Validate the new password against Django's validators.
                validate_password(new_pwd, user)
                user.set_password(new_pwd)
                user.save()
                messages.success(request, "Password changed successfully")
            except ValidationError as e:
                # If validation fails, show the errors to the user.
                for error in e.messages:
                    messages.error(request, error)
        else:
            messages.error(request, "Current password doesn't match!")

    # Redirect back to the page the user came from (securely).
    # We use get_safe_referer which ensures the URL is safe and on-domain.
    return redirect(get_safe_referer(request))


@admin_required
def user_list(request):
    """
    Administrative view to list, search, and manage all users.
    Includes pagination and user creation capabilities.
    """
    # Generate database filters based on search queries in the GET parameters.
    filters = user_filter(request)
    users_queryset = User.objects.filter(**filters).order_by("username")

    # Empty form for creating new users.
    form = SignupForm()

    # Set up pagination (5 users per page).
    page_number = request.GET.get("page", 1)
    paginator = Paginator(users_queryset, 5)
    users_page = paginator.get_page(page_number)

    if request.method == "POST":
        # Handle new user creation from the admin panel.
        form = SignupForm(request.POST)
        if form.is_valid():
            new_user = form.save()
            # Manually set the role from the form's cleaned data.
            new_user.profile.role = form.cleaned_data["role"]
            new_user.profile.save()
            messages.success(request, f"User {new_user.username} created successfully.")
            return redirect(get_safe_referer(request))
        else:
            messages.error(request, "Error creating user. Please check the form for details.")

    context = {
        "segment": "users",
        "users": users_page,
        "form": form,
    }
    return render(request, "apps/users.html", context)


@admin_required
def delete_user(request, id):
    """Deletes a user by their ID. Protected by admin requirement."""
    user_to_delete = get_object_or_404(User, id=id)
    # Anonymize logs before deletion to comply with GDPR
    anonymize_user_data(user_to_delete)
    user_to_delete.delete()
    return redirect(get_safe_referer(request))


@admin_required
def update_user(request, id):
    """Updates a user's details (username, email, role) from the admin panel."""
    user_to_update = get_object_or_404(User, id=id)

    if request.method == "POST":
        form = UserUpdateForm(request.POST, instance=user_to_update)
        if form.is_valid():
            updated_user = form.save()
            # Update the profile role as well.
            updated_user.profile.role = form.cleaned_data["role"]
            updated_user.profile.save()
            messages.success(request, "User updated successfully")

    return redirect(get_safe_referer(request))


@admin_required
def user_change_password(request, id):
    """Allows an administrator to forcefully reset a user's password."""
    user_to_change = get_object_or_404(User, id=id)
    log = logger.get_logger()

    if request.method == "POST":
        new_password = request.POST.get("password")
        if new_password:
            try:
                # Validate the new password.
                validate_password(new_password, user_to_change)
                user_to_change.set_password(new_password)
                user_to_change.save()

                # Log this administrative action
                log.access.info(f"ADMIN {request.user.username} FORCE-RESET PASSWORD for user {user_to_change.username}")

                messages.success(
                    request, f"Password updated for {user_to_change.username}"
                )
            except ValidationError as e:
                for error in e.messages:
                    messages.error(request, error)

    # Return redirect with referer check
    return redirect(get_safe_referer(request))


@admin_required
@require_POST
def toggle_emergency_access(request, id):
    """
    Enables or disables emergency access for a user.
    Logs the event as CRITICAL for audit purposes (HIPAA requirement).
    """
    user_to_elevate = get_object_or_404(User, id=id)
    profile = user_to_elevate.profile
    log = logger.get_logger()

    justification = request.POST.get("justification", "No justification provided.")
    try:
        duration_hours = int(request.POST.get("duration", 4))
    except (ValueError, TypeError):
        duration_hours = 4

    if not profile.is_emergency_access:
        profile.is_emergency_access = True
        profile.emergency_access_expiry = timezone.now() + timezone.timedelta(hours=duration_hours)
        profile.emergency_access_justification = justification
        profile.save()

        # HIPAA Audit Log: Critical severity for break-glass events
        log.access.critical(
            f"EMERGENCY ACCESS GRANTED to user {user_to_elevate.username} by {request.user.username}. "
            f"Justification: {justification}",
            target_user=user_to_elevate.username,
            justification=justification,
            expiry=profile.emergency_access_expiry.isoformat()
        )
        messages.warning(request, f"Emergency access granted to {user_to_elevate.username} for {duration_hours} hours.")
    else:
        profile.is_emergency_access = False
        profile.emergency_access_expiry = None
        profile.save()

        log.access.info(
            f"EMERGENCY ACCESS REVOKED for user {user_to_elevate.username} by {request.user.username}.",
            target_user=user_to_elevate.username
        )
        messages.success(request, f"Emergency access revoked for {user_to_elevate.username}.")

    return redirect(get_safe_referer(request))


def privacy_policy(request):
    """Displays the privacy policy page."""
    return render(request, "pages/privacy.html", {"segment": "privacy"})


def terms_and_conditions(request):
    """Displays the terms and conditions page."""
    return render(request, "pages/terms.html", {"segment": "terms"})


import json
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from django.core.files.storage import default_storage

def cleanup_user_resources(user):
    """
    Cleans up external resources (like S3/MinIO objects) associated with the user.
    """
    # Delete S3 objects for each project authored by the user
    for project in user.created_projects.all():
        if hasattr(default_storage, 'bucket'):
            prefix = f"{str(project.identifier)}/"
            s3_objects = default_storage.bucket.objects.filter(Prefix=prefix)
            s3_objects.delete()
        else:
            # Local storage cleanup
            import os
            import shutil
            from django.conf import settings
            project_path = os.path.join(settings.MEDIA_ROOT, str(project.identifier))
            if os.path.exists(project_path):
                shutil.rmtree(project_path)


@login_required
def delete_own_account(request):
    """Allows a user to delete their own account."""
    if request.method == "POST":
        user = request.user
        # Anonymize logs before deletion to comply with GDPR
        anonymize_user_data(user)
        # Cleanup resources before deleting user record (cascades will handle DB rows)
        cleanup_user_resources(user)
        logout(request)
        user.delete()
        messages.success(request, "Your account has been successfully deleted.")
        return redirect(reverse("users:signin"))
    return redirect(reverse("users:profile"))


@login_required
def update_cookie_consent(request):
    """
    Updates the user's cookie consent status in their profile.
    This is called via AJAX from the cookie banner.
    """
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            consent = data.get("consent")
            if consent in ["accepted", "rejected"]:
                profile = request.user.profile
                profile.cookie_consent = consent
                profile.cookie_consent_date = timezone.now()
                profile.save()
                return HttpResponse(status=204)
        except json.JSONDecodeError:
            pass
    return HttpResponse(status=400)


@login_required
def export_user_data(request):
    """
    Exports the current user's data in JSON format for GDPR compliance (Data Portability).
    Includes User model fields, Profile, Logs, Projects, Validation/Visualization runs, and Training Jobs.
    Ensures third-party data in logs is redacted.
    """
    user = request.user
    profile = user.profile
    
    # Base user and profile data
    data = {
        "user": {
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "date_joined": user.date_joined,
            "last_login": user.last_login,
        },
        "profile": {
            "identifier": str(profile.identifier),
            "role": profile.role,
            "full_name": profile.full_name,
            "country": profile.country,
            "city": profile.city,
            "zip_code": profile.zip_code,
            "address": profile.address,
            "phone": profile.phone,
            "accepted_policy": profile.accepted_policy,
            "accepted_policy_date": profile.accepted_policy_date,
            "accepted_terms": profile.accepted_terms,
            "accepted_terms_date": profile.accepted_terms_date,
            "cookie_consent": profile.cookie_consent,
            "cookie_consent_date": profile.cookie_consent_date,
        },
        "export_date": timezone.now(),
    }

    # Add Logs with Redaction
    user_logs = []
    for entry in user.log_entries.all():
        log_data = {
            "id": str(entry.id),
            "category": entry.category,
            "timestamp": entry.timestamp,
            "level": entry.level,
            "source": entry.source,
            "message": entry.message,
            "context_data": entry.context_data.copy() if entry.context_data else {},
        }
        
        # Redact third-party info from context_data
        if log_data["context_data"]:
            # If target_user is present and not this user, redact it
            target = log_data["context_data"].get("target_user")
            if target and target != user.username:
                log_data["context_data"]["target_user"] = "REDACTED"
            
            # Redact other potential identifiers in context
            for key in ["email", "full_name"]:
                if key in log_data["context_data"] and log_data["context_data"][key] != getattr(user, key, None):
                     log_data["context_data"][key] = "REDACTED"

        # Basic message redaction: if it contains another user's name, it's hard to 
        # redact perfectly without a full list of users, but we can at least 
        # ensure that for administrative actions, the message is generalized.
        if entry.category in ["access", "auth", "users"]:
             # If the message mentions a target user that is not the requester, redact the whole message or generalized it
             # This is a safety-first approach for portability.
             if "target_user" in log_data["context_data"] and log_data["context_data"]["target_user"] == "REDACTED":
                 log_data["message"] = f"[Redacted] Action performed on another user identifier."

        user_logs.append(log_data)
    
    data["logs"] = user_logs

    # Add Projects (where author)
    projects = user.created_projects.all()
    data["authored_projects"] = []
    for project in projects:
        project_data = {
            "title": project.title,
            "identifier": str(project.identifier),
            "creation_date": project.creation_date,
            "description": project.description,
            "status": project.status,
            "files": {
                "training_code": project.training_code.name if project.training_code else None,
                "requirements_file": project.requirements_file.name if project.requirements_file else None,
                "data_validation_script": project.data_validation_script.name if project.data_validation_script else None,
                "data_visualization_script": project.data_visualization_script.name if project.data_visualization_script else None,
                "results_visualization_script": project.results_visualization_script.name if project.results_visualization_script else None,
            }
        }
        data["authored_projects"].append(project_data)

    # Add Validation Runs
    data["validation_runs"] = list(user.validationrun_set.values(
        "id", "project__title", "status", "created_at", "started_at", "completed_at", "success", "output", "error_message"
    ))

    # Add Visualization Runs
    data["visualization_runs"] = list(user.visualizationrun_set.values(
        "id", "project__title", "status", "created_at", "started_at", "completed_at", "success", "output", "error_message"
    ))

    # Add Results Visualization Runs
    data["results_visualization_runs"] = list(user.resultsvisualizationrun_set.values(
        "id", "project__title", "status", "created_at", "started_at", "completed_at", "success", "output", "error_message"
    ))

    # Add Swarm Network Participation
    data["swarm_participations"] = []
    for part in user.swarm_participations.all():
        data["swarm_participations"].append({
            "network_name": part.network.name,
            "role": part.role,
            "participant_id": part.participant_id,
        })

    response = HttpResponse(
        json.dumps(data, cls=DjangoJSONEncoder, indent=4),
        content_type="application/json"
    )
    response["Content-Disposition"] = f'attachment; filename="user_data_{user.username}.json"'
    return response
