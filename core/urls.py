"""
Main URL configuration for the SwarmCloud project.
This module maps top-level URL paths to their respective application-specific
URL configurations. It also handles serving static and media files.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from common.views import privacy_policy, terms_and_conditions, license, imprint, contact

# List of root URL patterns for the entire project.
urlpatterns = [
    # Homepage and common marketing pages.
    path("", include("home.urls")),
    # Legal and Privacy
    path("privacy/", privacy_policy, name="privacy"),
    path("terms/", terms_and_conditions, name="terms"),
    path("license/", license, name="license"),
    path("imprint/", imprint, name="imprint"),
    path("contact/", contact, name="contact"),
    # Django Administrative interface.
    path("admin/", admin.site.urls),
    # User authentication, profile, and management.
    path("users/", include("users.urls")),
    # Collaborative project management and configuration.
    path("project/", include("project.urls")),
    # Dataset management and file uploads.
    path("data/", include("data.urls")),
    # Network provisioning and infrastructure status.
    path("network/", include("network.urls")),
    # Training job submission and monitoring.
    path("training/", include("training.urls")),
    # Result synchronization and visualization.
    path("results/", include("results.urls")),
    # Centralized project and system logs.
    path("logs/", include("logs.urls")),
    # Internal messaging and communication between participants.
    path("communication/", include("communication.urls")),
]

# Append static file serving helpers for development mode.
# This ensures that MEDIA_URL (e.g., /media/) points to MEDIA_ROOT.
if settings.DEBUG:
    urlpatterns += [
        path("__debug__/", include("debug_toolbar.urls")),
    ]
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)


# Use project-level error handlers so custom templates are rendered
# even during local development (DEBUG=True) and in production.
handler403 = "apps.common.views.permission_denied_view"
handler404 = "apps.common.views.page_not_found_view"
handler500 = "apps.common.views.server_error_view"
handler400 = "apps.common.views.bad_request_view"
