"""
URL configuration for the home application.
Maps web addresses to the view functions that handle the dashboard
and the starter page.
"""

from django.urls import path

from . import views

# Application namespace for home-related URLs.
app_name = "home"

urlpatterns = [
    # Dashboard view - the main entry point after login.
    path("", views.index, name="dashboard"),
    # A simple starter/test page.
    path("starter", views.starter, name="starter"),
]
