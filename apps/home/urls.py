"""URL configuration for the home app.

Maps web addresses to the view functions that handle the dashboard
and the starter page.
"""

from django.urls import path

from . import views

app_name = "home"

urlpatterns = [
    path("", views.index, name="dashboard"),
    path("starter", views.starter, name="starter"),
]
