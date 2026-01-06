"""
View functions for the project application.
Handles displaying projects, creating new ones, editing existing ones,
and managing the 'active project' context for each user.
"""

import json
import re
import uuid

from common.utils import get_s3_download_url
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import models
from django.http import (
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from logs import logger
from network.models import UserCurrentNetwork
from users.models import Profile

from .forms import ProjectForm
from .models import Project, UserCurrentProject
from .utils import handle_training_code_upload, process_member_identifiers


@login_required
def project_list(request):
    """
    Displays a list of all projects where the current user is
    either the author or a member. Separates active and archived projects.
    Cached for 2 minutes to reduce database load.
    """
    user_id = request.user.id
    cache_key = f"project_list_{user_id}"

    # Try to get cached data
    cached_data = cache.get(cache_key)
    if cached_data is not None:
        return render(request, "apps/project/project.html", cached_data)

    # Fetch active projects where the user is the author OR is in the members list.
    # Optimization: select_related for author (FK) and prefetch_related for members (M2M)
    active_projects = (
        Project.objects.filter(
            models.Q(author=request.user) | models.Q(members=request.user)
        )
        .exclude(status="ARCHIVED")
        .distinct()
        .select_related("author")
        .prefetch_related("members")
        .order_by("-created_at")
    )

    # Archived projects: Only show projects where the user is the AUTHOR and status is ARCHIVED.
    # Optimization: select_related for author
    archived_projects = (
        Project.objects.filter(author=request.user, status="ARCHIVED")
        .select_related("author")
        .order_by("-created_at")
    )

    # Try to find which project the user has set as their 'current' project.
    try:
        # Optimization: select_related for the project relation
        current_project_relation = UserCurrentProject.objects.select_related(
            "project"
        ).get(user=request.user)
        current_project = current_project_relation.project
    except UserCurrentProject.DoesNotExist:
        current_project = None

    # Calculate some basic statistics for the dashboard UI.
    total_active = active_projects.count()
    finished_projects_count = archived_projects.count()
    projects_to_do_count = total_active  # Assuming 'to do' means active

    context = {
        "segment": "project",
        "projects": active_projects,
        "archived_projects": archived_projects,
        "current_project": current_project,
        "finished_projects_count": finished_projects_count,
        "projects_to_do_count": projects_to_do_count,
        "total_projects": total_active + finished_projects_count,
    }

    # Cache for 2 minutes (120 seconds)
    cache.set(cache_key, context, 120)

    return render(request, "apps/project/project.html", context)


@login_required
def project_create(request):
    """
    Handles the creation of a new project through a form.
    """
    # Initialize a logger to track project-related activities.
    log = logger.get_logger(user=request.user, project=None)

    if request.method == "POST":
        # If the user submitted the form, populate it with POST data and
        # uploaded files.
        form = ProjectForm(request.POST, request.FILES)
        if form.is_valid():
            # commit=False allows us to modify the object before saving to
            # database.
            project = form.save(commit=False)
            project.author = request.user

            # Save the project to generate a primary key and identifier.
            project.save()

            # Process the list of member UUIDs provided in the form.
            process_member_identifiers(
                project, form.cleaned_data.get("member_identifiers", "")
            )

            # Some files might be uploaded as a collection in a directory structure.
            # handle_training_code_upload takes care of this complex file
            # handling.
            try:
                handle_training_code_upload(project, request)
            except Exception as e:
                # Log an error if something goes wrong during file processing.
                log.project.error(
                    f"ERROR PROCESSING TRAINING CODE UPLOAD - {project.title}: {str(e)}"
                )

            # Log successful project creation.
            log.project.info(f"PROJECT CREATED SUCCESSFULLY - {project.title}")

            return redirect("project:project_list")
    else:
        # If it's a GET request, provide an empty form to the user.
        form = ProjectForm()

    context = {
        "segment": "project",
        "form": form,
    }
    return render(request, "apps/project/new_project.html", context)


@login_required
def project_edit(request, pk):
    """
    Allows the project author to edit project details and files.
    """
    # Retrieve the project by its ID or return 404 if not found.
    project = get_object_or_404(Project, pk=pk)
    log = logger.get_logger(user=request.user, project=project)

    # Security check: Only the author of the project can edit it.
    if request.user != project.author:
        log.access.warning(
            f"Unauthorized edit attempt for project '{project.title}' by user {request.user.username}"
        )
        return redirect("project:project_list")

    if request.method == "POST":
        # Provide the existing 'instance' so the form updates it instead of
        # creating a new one.
        form = ProjectForm(request.POST, request.FILES, instance=project)
        if form.is_valid():
            project = form.save(commit=False)
            project.save()

            # Update the member list based on new UUID inputs.
            process_member_identifiers(
                project, form.cleaned_data.get("member_identifiers", "")
            )

            # Update any uploaded training code files.
            handle_training_code_upload(project, request)

            # If users were removed from the project, we should unset it as
            # their 'current' project.
            UserCurrentProject.objects.filter(project=project).exclude(
                user=project.author
            ).exclude(user__in=project.members.all()).delete()

            log.project.info(f"Project updated successfully: {project.title}")

            return redirect("project:project_list")
    else:
        # Populate the form with current project data.
        form = ProjectForm(instance=project)

    context = {
        "segment": "project",
        "form": form,
        "edit": True,
    }
    return render(request, "apps/project/new_project.html", context)


@login_required
def project_delete(request, pk):
    """
    Deletes a project. Only the project author is permitted to do this.
    """
    project = get_object_or_404(Project, pk=pk)
    log = logger.get_logger(user=request.user, project=project)

    # Verify authorship before deletion.
    if request.user == project.author:
        project_title = project.title
        project.delete()
        log.project.warning(f"Project deleted successfully - {project_title}")
    else:
        log.access.warning(
            f"Unauthorized delete attempt for project '{project.title}' by user {request.user.username}"
        )

    return redirect("project:project_list")


@login_required
def set_current_project(request, pk):
    """
    Sets a specific project as the 'active' project for the logged-in user.
    """
    project = get_object_or_404(Project, pk=pk)
    log = logger.get_logger(user=request.user, project=project)

    # Check if the user has permission to access this project.
    is_author = project.author == request.user
    is_member = request.user in project.members.all()

    if not (is_author or is_member):
        log.access.warning(
            f"Unauthorized project access attempt: '{project.title}' by user {request.user.username}"
        )
        return redirect("project:project_list")

    # Update or create the UserCurrentProject record for this user.
    UserCurrentProject.objects.update_or_create(
        user=request.user, defaults={"project": project}
    )

    log.project.debug(
        f"User {request.user.username} set active project to: '{project.title}'"
    )

    # When switching projects, we clear the 'current network' as it's
    # project-specific.
    UserCurrentNetwork.objects.filter(user=request.user).delete()

    return redirect("project:project_list")


@login_required
def project_archive(request, pk):
    """
    Toggles the project status between 'ARCHIVED' and 'IN_PROGRESS'.
    Only the author can do this.
    """
    project = get_object_or_404(Project, pk=pk)
    log = logger.get_logger(user=request.user, project=project)

    if request.user == project.author:
        if project.status == "ARCHIVED":
            project.status = "IN_PROGRESS"
            messages.success(request, f"Project '{project.title}' unarchived.")
            log.project.info(f"Project '{project.title}' unarchived.")
        else:
            project.status = "ARCHIVED"
            # If the archived project was the current project, unset it
            UserCurrentProject.objects.filter(
                user=request.user, project=project
            ).delete()
            messages.success(request, f"Project '{project.title}' archived.")
            log.project.info(f"Project '{project.title}' archived.")
        project.save()
    else:
        log.access.warning(
            f"Unauthorized archive attempt for project '{project.title}' by user {request.user.username}"
        )

    return redirect("project:project_list")


@login_required
@require_POST
def get_user_emails(request):
    """
    API endpoint used for real-time validation of user UUIDs in the frontend.
    Expects a JSON payload with 'identifiers' (comma-separated UUID strings).
    """
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    member_identifiers = data.get("identifiers", "")
    emails = []

    if member_identifiers:
        # Split input by commas, spaces, or newlines using regex.
        identifiers = re.split(r"[,\n\s]+", member_identifiers)

        for identifier_str in identifiers:
            identifier_str = identifier_str.strip()
            if not identifier_str:
                continue

            try:
                # Convert string to UUID object.
                profile_uuid = uuid.UUID(identifier_str)
                # Lookup the Profile to find the associated User's email.
                profile = Profile.objects.get(identifier=profile_uuid)
                emails.append(
                    {
                        "uuid": identifier_str,
                        "email": profile.user.email,
                        "found": True,
                    }
                )
            except (ValueError, Profile.DoesNotExist):
                # If the UUID is invalid or doesn't exist, inform the frontend.
                emails.append(
                    {
                        "uuid": identifier_str,
                        "email": "Not found",
                        "found": False,
                    }
                )

    return JsonResponse({"emails": emails})


@login_required
def download_project_file(request, pk, file_type):
    """
    Generates a presigned URL for a project file and redirects to it.
    Ensures the user has access to the project.
    """
    project = get_object_or_404(Project, pk=pk)

    # Check if the user has permission to access this project.
    is_author = project.author == request.user
    is_member = request.user in project.members.all()

    if not (is_author or is_member):
        return HttpResponseForbidden(
            "You do not have permission to access this project."
        )

    # Get the file field based on file_type
    file_field = getattr(project, file_type, None)

    if not file_field or not file_field.name:
        return redirect("project:project_edit", pk=pk)

    # Use the name (which is the S3 key) to generate a presigned URL
    download_url = get_s3_download_url(file_field.name)

    # Security: Validate the redirect URL host
    from urllib.parse import urlparse

    parsed_url = urlparse(download_url)
    allowed_hosts = [
        urlparse(settings.AWS_S3_ENDPOINT_URL).netloc,
        urlparse(settings.PUBLIC_URL).netloc,
    ]

    # Allow configured S3 hosts or standard AWS S3 domains
    if (
        parsed_url.netloc not in allowed_hosts
        and not parsed_url.netloc.endswith("amazonaws.com")
    ):
        return HttpResponseForbidden("External URL forbidden")

    return HttpResponseRedirect(download_url)
