from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="dashboard"),
    path("starter", views.starter, name="starter"),
    ##
    path('upload/', views.upload_document, name='upload_document'),
]
