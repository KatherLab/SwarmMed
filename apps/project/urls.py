from django.urls import path

from . import views

urlpatterns = [
    path("", views.project, name="project"),
    path("new/", views.new_project, name="new_project"),

]