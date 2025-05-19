from django.urls import path

from . import views

urlpatterns = [
    path("", views.network, name="network"),
    path("new/", views.new_network, name="new_network"),

]