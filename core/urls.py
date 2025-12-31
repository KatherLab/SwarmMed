"""
Main URL configuration for the SwarmCloud project.
This module maps top-level URL paths to their respective application-specific
URL configurations. It also handles serving static and media files.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

# List of root URL patterns for the entire project.
urlpatterns = [
    # Homepage and common marketing pages.
    path("", include("home.urls")),

    # Django Administrative interface.
    path("admin/", admin.site.urls),

    # User authentication, profile, and management.
    path("users/", include("apps.users.urls")),

    # Collaborative project management and configuration.
    path("project/", include("apps.project.urls")),

    # Dataset management and file uploads.
    path("data/", include("apps.data.urls")),

    # Network provisioning and infrastructure status.
    path("network/", include("apps.network.urls")),

    # Training job submission and monitoring.
    path("training/", include("apps.training.urls")),

    # Result synchronization and visualization.
    path("results/", include("apps.results.urls")),

    # Centralized project and system logs.
    path("logs/", include("apps.logs.urls")),

    # Internal messaging and communication between participants.
    path("communication/", include("apps.communication.urls")),

    # Django Debug Toolbar endpoint.
    path("__debug__/", include("debug_toolbar.urls")),

    # Manual re_path patterns to serve media and static files when
    # DEBUG=False if needed (e.g., during some containerized deployments).
    re_path(
        r'^media/(?P<path>.*)$',
        serve,
        {'document_root': settings.MEDIA_ROOT}
    ),
    re_path(
        r'^static/(?P<path>.*)$',
        serve,
        {'document_root': settings.STATIC_ROOT}
    ),
]

# Append static file serving helpers for development mode.
# This ensures that MEDIA_URL (e.g., /media/) points to MEDIA_ROOT.
if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT
    )
    urlpatterns += static(
        settings.STATIC_URL,
        document_root=settings.STATIC_ROOT
    )
