"""URL configuration for the results application.
Maps web addresses to view functions for viewing results, downloading files,
and managing visualization runs.
"""

from django.urls import path

from . import explain_views, views

# Application namespace for results-related URLs.
app_name = "results"

# Standard list of URL patterns for the results app.
urlpatterns = [
    # Main results dashboard.
    path("", views.results, name="results"),
    # Download a specific result file by its database ID.
    path(
        "export/result/<int:result_id>/",
        views.download_result,
        name="download_result",
    ),
    # Download all results for a project (optionally filtered by job) as a ZIP.
    path(
        "export/all/<str:project_id>/",
        views.download_all_results,
        name="download_all_results",
    ),
    # Download a result using its S3 key.
    path(
        "export/by-key/",
        views.download_result_by_key,
        name="download_result_by_key",
    ),
    # --- Results Visualization Endpoints ---
    # Start a visualization background task for a specific job.
    path(
        "visualize/start/<str:job_id>/",
        views.start_results_visualization,
        name="start_results_visualization",
    ),
    # Stop a running visualization task.
    path(
        "visualize/stop/",
        views.stop_results_visualization,
        name="stop_results_visualization",
    ),
    # Poll for the current status and generated plots of a visualization run.
    path(
        "visualize/status/<str:job_id>/",
        views.results_visualization_status,
        name="results_visualization_status",
    ),
    # Serve visualization plots via Django proxy
    path(
        "plot/<str:plot_id>/<str:plot_type>/",
        views.get_visualization_plot,
        name="get_visualization_plot",
    ),
    # --- Explainability Endpoints ---
    # (No URL argument may be called pk, identifier, project_id or network_id:
    # project_membership_required would read it as a project.)
    path(
        "explain/start/<str:job_id>/",
        explain_views.start_explanation,
        name="start_explanation",
    ),
    path("explain/stop/", explain_views.stop_explanation, name="stop_explanation"),
    path(
        "explain/delete/",
        explain_views.delete_explanation,
        name="delete_explanation",
    ),
    path(
        "explain/status/<str:job_id>/",
        explain_views.explanation_status,
        name="explanation_status",
    ),
    path(
        "explain/run/<str:run_uuid>/cases/",
        explain_views.explanation_cases,
        name="explanation_cases",
    ),
    path(
        "explain/run/<str:run_uuid>/mode/",
        explain_views.explanation_mode,
        name="explanation_mode",
    ),
    path(
        "explain/run/<str:run_uuid>/download/",
        explain_views.explanation_download,
        name="explanation_download",
    ),
    path(
        "explain/image/<str:image_kind>/<str:image_uuid>/",
        explain_views.explanation_image,
        name="explanation_image",
    ),
    path(
        "explain/case/<str:case_uuid>/reviews/",
        explain_views.explanation_reviews,
        name="explanation_reviews",
    ),
]
