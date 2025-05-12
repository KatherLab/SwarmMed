from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="dashboard"),
    path("starter", views.starter, name="starter"),
    path("data/", views.data, name="data"),
    path("project/", views.project, name="project"),
    path("project/new/", views.new_project, name="new_project"),
    path("network/", views.network, name="network"),
    path("training/", views.training, name="training"),
    path("results/", views.results, name="results"),
    path("logs/", views.logs, name="logs"),
    ##
    path('upload/', views.upload_document, name='upload_document'),
]
