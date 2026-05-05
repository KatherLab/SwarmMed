"""Common views for the application, including legal and static pages."""

from django.conf import settings
from django.contrib.auth.models import User
from django.shortcuts import render

from logs.models import LogEntry
from project.models import Project
from training.models import TrainingJob


def privacy_policy(request):
    """Displays the privacy policy page.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: The rendered privacy policy page.
    """
    return render(
        request,
        "apps/common/privacy.html",
        {"segment": "privacy"},
    )


def terms_and_conditions(request):
    """Displays the terms and conditions page.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: The rendered terms and conditions page.
    """
    return render(request, "apps/common/terms.html", {"segment": "terms"})


def license(request):
    """Displays the license page.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: The rendered license page.
    """
    return render(request, "apps/common/license.html", {"segment": "license"})


def imprint(request):
    """Displays the imprint page.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: The rendered imprint page.
    """
    return render(request, "apps/common/imprint.html", {"segment": "imprint"})


def contact(request):
    """Displays the contact page.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: The rendered contact page.
    """
    return render(request, "apps/common/contact.html", {"segment": "contact"})


def dashboard_callback(request, context):
    """Callback for django-unfold dashboard.

    Enriches the admin dashboard with system statistics.

    Args:
        request (HttpRequest): The incoming HTTP request.
        context (dict): The current context dictionary.

    Returns:
        dict: The updated context dictionary with stats.
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
    """Render a custom 403 (Permission Denied) page.

    This view is registered as the project's `handler403` so the
    `templates/errors/403.html` file is used even when `DEBUG` is True for
    easier local development and consistent UX in production.

    Args:
        request (HttpRequest): The incoming HTTP request.
        exception (Exception, optional): The exception that triggered the error.

    Returns:
        HttpResponse: The rendered 403 error page.
    """
    return render(request, "errors/403.html", status=403)


def page_not_found_view(request, exception=None):
    """Render a custom 404 (Not Found) page.

    Args:
        request (HttpRequest): The incoming HTTP request.
        exception (Exception, optional): The exception that triggered the error.

    Returns:
        HttpResponse: The rendered 404 error page.
    """
    return render(request, "errors/404.html", status=404)


def server_error_view(request):
    """Render a custom 500 (Server Error) page.

    Args:
        request (HttpRequest): The incoming HTTP request.

    Returns:
        HttpResponse: The rendered 500 error page.
    """
    return render(request, "errors/500.html", status=500)


def bad_request_view(request, exception=None):
    """Render a custom 400 (Bad Request) page.

    Args:
        request (HttpRequest): The incoming HTTP request.
        exception (Exception, optional): The exception that triggered the error.

    Returns:
        HttpResponse: The rendered 400 error page.
    """
    return render(request, "errors/400.html", status=400)
