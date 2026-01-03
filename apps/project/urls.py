"""
URL configuration for the project application.
Maps web addresses to the view functions that handle project listing,
creation, editing, deletion, and context management.
"""

from django.urls import path

from . import views

# Application namespace for project-related URLs.
# This allows using 'project:project_list' in templates and redirects.
app_name = "project"

urlpatterns = [
    # Dashboard view listing all projects the user is involved in.
    path("", views.project_list, name="project_list"),
    # Form to create a brand new project.
    path("new/", views.project_create, name="project_create"),
    # Form to edit an existing project, identified by its numeric primary key
    # (pk).
    path("edit/<int:pk>/", views.project_edit, name="project_edit"),
    # Endpoint to delete a project.
    path("delete/<int:pk>/", views.project_delete, name="project_delete"),
    # Sets a project as the 'active' context for the logged-in user.
    path(
        "set-current/<int:pk>/", views.set_current_project, name="set_current_project"
    ),
    # Marks a project's status as 'Archived'.
    path("archive/<int:pk>/", views.project_archive, name="project_archive"),
    # AJAX/API endpoint for fetching user emails based on UUIDs.
    path("get-user-emails/", views.get_user_emails, name="get_user_emails"),
    # Endpoint for downloading project files via presigned URLs.
    path(
        "download/<int:pk>/<str:file_type>/",
        views.download_project_file,
        name="download_file",
    ),
]
