"""
Common views for the application, including legal and static pages.
"""

from django.shortcuts import render
from django.contrib.auth.models import User
from project.models import Project
from training.models import TrainingJob
from logs.models import LogEntry


def privacy_policy(request):
    """Displays the privacy policy page."""
    return render(request, "apps/common/privacy.html", {"segment": "privacy"})


def terms_and_conditions(request):
    """Displays the terms and conditions page."""
    return render(request, "apps/common/terms.html", {"segment": "terms"})


def license(request):
    """Displays the license page."""
    return render(request, "apps/common/license.html", {"segment": "license"})


def imprint(request):
    """Displays the imprint page."""
    return render(request, "apps/common/imprint.html", {"segment": "imprint"})


def contact(request):
    """Displays the contact page."""
    return render(request, "apps/common/contact.html", {"segment": "contact"})


def dashboard_callback(request, context):
    """
    Callback for django-unfold dashboard.
    Enriches the admin dashboard with system statistics.
    """
    context.update(
        {
            "total_users": User.objects.count(),
            "total_projects": Project.objects.count(),
            "active_training_jobs": TrainingJob.objects.filter(
                status__in=["RUNNING", "STARTING"]
            ).count(),
            "recent_logs": LogEntry.objects.all()[:5],
            "stats": [
                {
                    "title": "Total Users",
                    "metric": User.objects.count(),
                    "footer": "Registered researchers",
                },
                {
                    "title": "Total Projects",
                    "metric": Project.objects.count(),
                    "footer": "Collaborative swarms",
                },
                {
                    "title": "Training Jobs",
                    "metric": TrainingJob.objects.count(),
                    "footer": "ML executions",
                },
            ],
        }
    )
    return context


def permission_denied_view(request, exception=None):
    """
    Render a custom 403 (Permission Denied) page.

    This view is registered as the project's `handler403` so the
    `templates/errors/403.html` file is used even when `DEBUG` is True for
    easier local development and consistent UX in production.
    """
    return render(request, "errors/403.html", status=403)


def page_not_found_view(request, exception=None):
    """Render a custom 404 (Not Found) page."""
    return render(request, "errors/404.html", status=404)


def server_error_view(request):
    """Render a custom 500 (Server Error) page."""
    return render(request, "errors/500.html", status=500)


def bad_request_view(request, exception=None):
    """Render a custom 400 (Bad Request) page."""
    return render(request, "errors/400.html", status=400)
