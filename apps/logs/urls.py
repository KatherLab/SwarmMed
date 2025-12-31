"""
URL configuration for the logs app.
Maps web addresses to the view functions that display logs and handle downloads.
"""

from django.urls import path

from . import views

# Set the namespace for this app's URLs
app_name = 'logs'

urlpatterns = [
    # Main logs dashboard view
    path("", views.logs, name="logs"),

    # Endpoint to download logs for a specific category as a text file
    path(
        'download/<str:category_key>/',
        views.download_log_category,
        name='download_log_category'
    ),
]
