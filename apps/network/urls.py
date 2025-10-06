from django.urls import path

from . import views

urlpatterns = [
    path("", views.network, name="network"),
    path("new/", views.new_network, name="new_network"),
    path("<uuid:network_id>/", views.network_detail, name="network_detail"),
    path("<uuid:network_id>/start/", views.start_swarm_network, name="start_swarm_network"),
    path("<uuid:network_id>/delete/", views.delete_swarm_network, name="delete_swarm_network"),

]