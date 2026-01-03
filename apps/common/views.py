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
