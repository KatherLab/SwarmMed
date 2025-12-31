"""
URL configuration for the results application.
Maps web addresses to view functions for viewing results, downloading files,
and managing visualization runs.
"""

from django.urls import path

from . import views

# Application namespace for results-related URLs.
app_name = 'results'

# Standard list of URL patterns for the results app.
urlpatterns = [
    # Main results dashboard.
    path("", views.results, name="results"),

    # Download a specific result file by its database ID.
    path(
        'download/result/<int:result_id>/',
        views.download_result,
        name='download_result'
    ),

    # Download all results for a project (optionally filtered by job) as a ZIP.
    path(
        'download/all/<str:project_id>/',
        views.download_all_results,
        name='download_all_results'
    ),

    # Download a result using its S3 key.
    path(
        'download/by-key/',
        views.download_result_by_key,
        name='download_result_by_key'
    ),

    # --- Results Visualization Endpoints ---

    # Start a visualization background task for a specific job.
    path(
        'visualize/start/<str:job_id>/',
        views.start_results_visualization,
        name='start_results_visualization'
    ),

    # Stop a running visualization task.
    path(
        'visualize/stop/',
        views.stop_results_visualization,
        name='stop_results_visualization'
    ),

    # Poll for the current status and generated plots of a visualization run.
    path(
        'visualize/status/<str:job_id>/',
        views.results_visualization_status,
        name='results_visualization_status'
    ),
]
