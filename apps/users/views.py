"""
View functions for the users application.
Handles authentication (sign in, sign up, sign out), password management,
profile updates, and administrative user management.
"""

import json

from common.utils import get_safe_referer
from django.contrib import messages
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.views import (
    PasswordChangeView,
    PasswordResetConfirmView,
    PasswordResetView,
)
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from logs import logger

from users.forms import (
    AdminAddUserForm,
    ProfileForm,
    UserPasswordChangeForm,
    UserPasswordResetForm,
    UserSetPasswordForm,
    UserUpdateForm,
)
from users.models import Profile
from users.utils import anonymize_user_data, user_filter

from .decorators import admin_required


def index(request):
    """Simple index view for users (mainly for testing)."""
    return HttpResponse("INDEX Users")


class UserPasswordChangeView(PasswordChangeView):
    """View to allow users to change their password while logged in."""

    template_name = "apps/users/auth/password-change.html"
    form_class = UserPasswordChangeForm


class UserPasswordResetView(PasswordResetView):
    """View to initiate the password reset process via email."""

    template_name = "apps/users/auth/forgot-password.html"
    form_class = UserPasswordResetForm
    success_url = reverse_lazy("users:password_reset_done")
    email_template_name = "apps/users/auth/password_reset_email.html"
    subject_template_name = "apps/users/auth/password_reset_subject.txt"


class UserPasswordResetConfirmView(PasswordResetConfirmView):
    """View to finalize password reset after clicking the email link."""

    template_name = "apps/users/auth/reset-password.html"
    form_class = UserSetPasswordForm
    success_url = reverse_lazy("users:password_reset_complete")


@login_required
def settings(request):
    """Displays and handles updates for the logged-in user's settings."""
    # Retrieve or create the Profile associated with the current user.
    user_profile, created = Profile.objects.get_or_create(
        user=request.user,
        defaults={"role": "admin" if request.user.is_superuser else "user"},
    )

    password_errors = {}

    # Initialize form with instance (default for GET)
    form = ProfileForm(instance=user_profile)

    if request.method == "POST":
        if "update_profile" in request.POST:
            # If the form was submitted, process the POST data.
            form = ProfileForm(request.POST, instance=user_profile)
            if form.is_valid():
                form.save()
                messages.success(request, "Settings updated successfully")
                return redirect("users:settings")

        elif "update_password" in request.POST:
            current_pwd = request.POST.get("current_password")
            new_pwd = request.POST.get("new_password")
            user = request.user

            password_valid = True

            # Verify current password
            if not check_password(current_pwd, user.password):
                password_errors["current_password"] = [
                    "Current password doesn't match!"
                ]
                password_valid = False

            if password_valid:
                try:
                    validate_password(new_pwd, user)
                    user.set_password(new_pwd)
                    user.save()
                    # Important: Keep the user logged in
                    update_session_auth_hash(request, user)
                    messages.success(request, "Password changed successfully")
                    return redirect("users:settings")
                except ValidationError as e:
                    password_errors["new_password"] = e.messages

            if password_errors:
                messages.error(request, "Please correct the errors below.")

    # Get the email of the first superuser as the DPO/Admin contact.
    admin_user = User.objects.filter(is_superuser=True).order_by("id").first()
    admin_email = (
        admin_user.email if admin_user else "admin@medswarmhub.example.com"
    )

    context = {
        "form": form,
        "segment": "settings",
        "admin_email": admin_email,
        "password_errors": password_errors,
    }
    return render(request, "apps/users/settings.html", context)


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
    form = AdminAddUserForm()
    form_errors = False
    password_error_id = request.GET.get("password_error")

    # Set up pagination (5 users per page).
    page_number = request.GET.get("page", 1)
    paginator = Paginator(users_queryset, 5)
    users_page = paginator.get_page(page_number)

    if request.method == "POST":
        # Handle new user creation from the admin panel.
        form = AdminAddUserForm(request.POST)
        if form.is_valid():
            new_user = form.save()
            # Manually set the role from the form's cleaned data.
            new_user.profile.role = form.cleaned_data["role"]
            new_user.profile.save()
            messages.success(
                request, f"User {new_user.username} created successfully."
            )
            return redirect(reverse("users:user_list"))
        else:
            form_errors = True
            messages.error(
                request,
                "Error creating user. Please check the form for details.",
            )

    context = {
        "segment": "users",
        "users": users_page,
        "form": form,
        "form_errors": form_errors,
        "password_error_id": password_error_id,
    }
    return render(request, "apps/users/user_list.html", context)


@admin_required
def delete_user(request, id):
    """Deletes a user by their ID. Protected by admin requirement."""
    user_to_delete = get_object_or_404(User, id=id)
    # Anonymize logs before deletion to comply with GDPR
    anonymize_user_data(user_to_delete)

    log = logger.get_logger()
    log.access.warning(
        f"ADMIN {request.user.username} DELETED user account: {user_to_delete.username}",
        target_user=user_to_delete.username,
    )

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
            return redirect(reverse("users:user_list"))
        else:
            messages.error(request, "Error updating user.")
            return redirect(reverse("users:user_list") + f"?update_error={id}")

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
                log.access.info(
                    f"ADMIN {request.user.username} FORCE-RESET PASSWORD for user {user_to_change.username}"
                )

                messages.success(
                    request, f"Password updated for {user_to_change.username}"
                )
                return redirect(reverse("users:user_list"))
            except ValidationError as e:
                for error in e.messages:
                    messages.error(request, error, extra_tags="password_error")
                return redirect(
                    reverse("users:user_list") + f"?password_error={id}"
                )
        else:
            messages.error(
                request,
                "Password cannot be empty.",
                extra_tags="password_error",
            )
            return redirect(
                reverse("users:user_list") + f"?password_error={id}"
            )

    # Return redirect with referer check
    return redirect(get_safe_referer(request))


@admin_required
@require_POST
def toggle_emergency_access(request, id):
    """
    Enables or disables emergency access for a user.
    Logs the event as CRITICAL for audit purposes (HIPAA requirement).
    Only superusers can perform this action to prevent recursive elevation.
    """
    if not request.user.is_superuser:
        log = logger.get_logger()
        log.access.warning(
            f"UNAUTHORIZED EMERGENCY ACCESS ATTEMPT by {request.user.username} for user ID {id}."
        )
        raise PermissionDenied

    user_to_elevate = get_object_or_404(User, id=id)
    profile = user_to_elevate.profile
    log = logger.get_logger()

    justification = request.POST.get(
        "justification", "No justification provided."
    )
    try:
        duration_hours = int(request.POST.get("duration", 4))
    except (ValueError, TypeError):
        duration_hours = 4

    if not profile.is_emergency_access:
        profile.is_emergency_access = True
        profile.emergency_access_expiry = timezone.now() + timezone.timedelta(
            hours=duration_hours
        )
        profile.emergency_access_justification = justification
        profile.save()

        # HIPAA Audit Log: Critical severity for break-glass events
        log.access.critical(
            f"EMERGENCY ACCESS GRANTED to user {user_to_elevate.username} by {request.user.username}. "
            f"Justification: {justification}",
            target_user=user_to_elevate.username,
            justification=justification,
            expiry=profile.emergency_access_expiry.isoformat(),
        )
        messages.warning(
            request,
            f"Emergency access granted to {user_to_elevate.username} for {duration_hours} hours.",
        )
    else:
        profile.is_emergency_access = False
        profile.emergency_access_expiry = None
        profile.save()

        log.access.info(
            f"EMERGENCY ACCESS REVOKED for user {user_to_elevate.username} by {request.user.username}.",
            target_user=user_to_elevate.username,
        )
        messages.success(
            request,
            f"Emergency access revoked for {user_to_elevate.username}.",
        )

    return redirect(get_safe_referer(request))


@login_required
def accept_terms(request):
    """
    Forces users to accept Terms and Privacy Policy before accessing the dashboard.
    """
    profile = request.user.profile

    # If already accepted, redirect to dashboard
    if profile.accepted_terms and profile.accepted_policy:
        return redirect("home:dashboard")

    if request.method == "POST":
        accept_terms = request.POST.get("accept_terms") == "on"
        accept_privacy = request.POST.get("accept_privacy") == "on"

        if accept_terms and accept_privacy:
            from django.utils import timezone

            profile.accepted_terms = True
            profile.accepted_terms_date = timezone.now()
            profile.accepted_policy = True
            profile.accepted_policy_date = timezone.now()
            profile.save()
            messages.success(
                request, "Thank you for accepting our legal terms."
            )
            return redirect("home:dashboard")
        else:
            messages.error(
                request, "You must accept both documents to continue."
            )

    return render(request, "apps/users/accept_terms.html")


def cleanup_user_resources(user):
    """
    Cleans up external resources (like S3/MinIO objects) associated with the user.
    """
    # Delete S3 objects for each project authored by the user
    # Optimize with select_related to avoid N+1 queries
    for project in user.created_projects.select_related("author").all():
        if hasattr(default_storage, "bucket"):
            prefix = f"{str(project.identifier)}/"
            s3_objects = default_storage.bucket.objects.filter(Prefix=prefix)
            s3_objects.delete()
        else:
            # Local storage cleanup
            import os
            import shutil

            from django.conf import settings

            project_path = os.path.join(
                settings.MEDIA_ROOT, str(project.identifier)
            )
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

        log = logger.get_logger()
        log.access.warning(f"User {user.username} DELETED their own account.")

        logout(request)
        user.delete()
        messages.success(
            request, "Your account has been successfully deleted."
        )
        return redirect(reverse("users:signin"))
    return redirect(reverse("users:settings"))


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

    log = logger.get_logger()
    log.access.info(
        f"User {user.username} EXPORTED their personal data (GDPR Portability)."
    )

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
    # Optimize query with select_related for related fields
    user_logs = []
    for entry in user.log_entries.select_related(
        "project", "swarm_network", "signing_key"
    ).all():
        log_data = {
            "id": str(entry.id),
            "category": entry.category,
            "timestamp": entry.timestamp,
            "level": entry.level,
            "source": entry.source,
            "message": entry.message,
            "context_data": (
                entry.context_data.copy() if entry.context_data else {}
            ),
        }

        # Redact third-party info from context_data
        if log_data["context_data"]:
            # If target_user is present and not this user, redact it
            target = log_data["context_data"].get("target_user")
            if target and target != user.username:
                log_data["context_data"]["target_user"] = "REDACTED"

            # Redact other potential identifiers in context
            for key in ["email", "full_name"]:
                if key in log_data["context_data"] and log_data[
                    "context_data"
                ][key] != getattr(user, key, None):
                    log_data["context_data"][key] = "REDACTED"

        # Basic message redaction: if it contains another user's name, it's hard to
        # redact perfectly without a full list of users, but we can at least
        # ensure that for administrative actions, the message is generalized.
        if entry.category in ["access", "auth", "users"]:
            # If the message mentions a target user that is not the requester, redact the whole message or generalized it
            # This is a safety-first approach for portability.
            if (
                "target_user" in log_data["context_data"]
                and log_data["context_data"]["target_user"] == "REDACTED"
            ):
                log_data["message"] = (
                    "[Redacted] Action performed on another user identifier."
                )

        user_logs.append(log_data)

    data["logs"] = user_logs

    # Add Projects (where author)
    # Optimize with prefetch_related for M2M relationships and select_related for FK
    projects = (
        user.created_projects.select_related("author")
        .prefetch_related(
            "members",
            "validation_runs",
            "visualization_runs",
            "training_jobs",
            "swarm_networks",
        )
        .all()
    )
    data["authored_projects"] = []
    for project in projects:
        project_data = {
            "title": project.title,
            "identifier": str(project.identifier),
            "created_at": project.created_at,
            "description": project.description,
            "status": project.status,
            "files": {
                "training_code": (
                    project.training_code.name
                    if project.training_code
                    else None
                ),
                "requirements_file": (
                    project.requirements_file.name
                    if project.requirements_file
                    else None
                ),
                "data_validation_script": (
                    project.data_validation_script.name
                    if project.data_validation_script
                    else None
                ),
                "data_visualization_script": (
                    project.data_visualization_script.name
                    if project.data_visualization_script
                    else None
                ),
                "results_visualization_script": (
                    project.results_visualization_script.name
                    if project.results_visualization_script
                    else None
                ),
            },
        }
        data["authored_projects"].append(project_data)

    # Add Validation Runs
    data["validation_runs"] = list(
        user.validationrun_set.values(
            "id",
            "project__title",
            "status",
            "created_at",
            "started_at",
            "completed_at",
            "success",
            "output",
            "error_message",
        )
    )

    # Add Visualization Runs
    data["visualization_runs"] = list(
        user.visualizationrun_set.values(
            "id",
            "project__title",
            "status",
            "created_at",
            "started_at",
            "completed_at",
            "success",
            "output",
            "error_message",
        )
    )

    # Add Results Visualization Runs
    data["results_visualization_runs"] = list(
        user.resultsvisualizationrun_set.values(
            "id",
            "project__title",
            "status",
            "created_at",
            "started_at",
            "completed_at",
            "success",
            "output",
            "error_message",
        )
    )

    # Add Swarm Network Participation
    # Optimize with select_related for related network and project
    data["swarm_participations"] = []
    for part in user.swarm_participations.select_related(
        "network__project", "user"
    ).all():
        data["swarm_participations"].append(
            {
                "network_name": part.network.name,
                "role": part.role,
                "participant_id": part.participant_id,
            }
        )

    response = HttpResponse(
        json.dumps(data, cls=DjangoJSONEncoder, indent=4),
        content_type="application/json",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="user_data_{user.username}.json"'
    )
    return response
