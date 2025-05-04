from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="dashboard"),
    path("starter", views.starter, name="starter"),
    path("data/", views.data, name="data"),
    path("network/", views.network, name="network"),
    path("training/", views.training, name="training"),
    path("logs/", views.logs, name="logs"),
]
