"""
URL configuration for the training application.
Maps web addresses to view functions for training management,
API status polling, and log streaming.
"""

from django.urls import path

from . import views

# Application namespace for training-related URLs.
app_name = "training"

# List of URL patterns for the training app.
urlpatterns = [
    # Main training dashboard.
    path("", views.training, name="training"),
    # Endpoint to initiate training on a specific swarm network.
    path("start/<uuid:network_id>/", views.start_training, name="start_training"),
    # Endpoint to manually abort a running training job.
    path("stop/<uuid:network_id>/", views.stop_training, name="stop_training"),
    # AJAX endpoint for polling the current job status and progress.
    path("status/", views.training_status_api, name="training_status_api"),
    # AJAX endpoint for fetching the latest execution logs.
    path("logs/", views.training_logs_api, name="training_logs_api"),
]
