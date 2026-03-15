"""
URL configuration for the data app.
Maps web addresses to the view functions that handle data browsing,
uploads, validation, and visualization.
"""

from django.urls import path

from . import views

# Set the namespace for this app's URLs
app_name = "data"

urlpatterns = [
    # Main data overview page
    path("", views.data, name="data"),
    # File and folder management
    path("files/", views.list_files, name="list_files"),
    path("download/", views.download_file, name="download_file"),
    path("upload/", views.upload_files, name="upload_files"),
    path("delete/", views.delete_file, name="delete_file"),
    path("rename/", views.rename_file, name="rename_file"),
    # JSON helper for folder-selection UI components
    path("folders/json/", views.list_all_folders, name="list_all_folders"),
    # Data Validation control and status
    path("validation/start/", views.start_validation, name="start_validation"),
    path("validation/stop/", views.stop_validation, name="stop_validation"),
    path(
        "validation/status/", views.validation_status, name="validation_status"
    ),
    # Manifest API for training containers
    path(
        "manifest/", views.get_project_manifest, name="get_project_manifest"
    ),
    # Data Visualization control and status
    path(
        "visualization/start/",
        views.start_visualization,
        name="start_visualization",
    ),
    path(
        "visualization/stop/",
        views.stop_visualization,
        name="stop_visualization",
    ),
    path(
        "visualization/status/",
        views.visualization_status,
        name="visualization_status",
    ),
    # Serve visualization plots via Django proxy
    path(
        "plot/<str:plot_id>/<str:plot_type>/",
        views.get_visualization_plot,
        name="get_visualization_plot",
    ),
]
