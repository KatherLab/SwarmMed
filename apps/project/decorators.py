from functools import wraps
from django.shortcuts import get_object_or_404, redirect, render
from .models import Project, UserCurrentProject


def project_membership_required(view_func):
    """
    Decorator for views that require a user to be either the author or a member
    of a specific project, identified by a 'pk' or 'identifier' in the URL.
    """

    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        project_id = (
            kwargs.get("pk") or kwargs.get("identifier") or kwargs.get("project_id")
        )

        if not project_id:
            # Fallback to checking the active project in session/DB
            return project_context_required(view_func)(request, *args, **kwargs)

        # Handle both UUID and integer IDs
        if isinstance(project_id, str) and len(project_id) > 10:  # Likely UUID
            project = get_object_or_404(Project, identifier=project_id)
        else:
            project = get_object_or_404(Project, pk=project_id)

        is_author = project.author == request.user
        is_member = project.members.filter(id=request.user.id).exists()

        if not (is_author or is_member):
            return redirect("project:project_list")

        return view_func(request, *args, **kwargs)

    return _wrapped_view


def project_context_required(view_func):
    """
    Decorator for views that rely on a 'current' project being selected
    via UserCurrentProject. Ensures the user is still a member.
    """

    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        try:
            current_project_relation = UserCurrentProject.objects.get(user=request.user)
            project = current_project_relation.project

            if not project:
                raise UserCurrentProject.DoesNotExist()

            # Verify membership (in case they were removed)
            is_author = project.author == request.user
            is_member = project.members.filter(id=request.user.id).exists()

            if not (is_author or is_member):
                current_project_relation.delete()
                return redirect("project:project_list")

        except UserCurrentProject.DoesNotExist:
            # Determine which app we are in for a better empty state
            segment = "data"
            if "network" in request.path:
                segment = "network"
            elif "results" in request.path:
                segment = "results"
            elif "training" in request.path:
                segment = "training"
            elif "logs" in request.path:
                segment = "logs"

            return render(
                request, "apps/project/no_project_selected.html", {"segment": segment}
            )

        return view_func(request, *args, **kwargs)

    return _wrapped_view
