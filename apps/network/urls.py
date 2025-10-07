from django.urls import path

from . import views

urlpatterns = [
    path("", views.network, name="network"),
    path("new/", views.new_network, name="new_network"),
    path("<uuid:network_id>/start/", views.start_swarm_network, name="start_swarm_network"),
    path("<uuid:network_id>/stop/", views.stop_swarm_network, name="stop_swarm_network"),
    path("<uuid:network_id>/delete/", views.delete_swarm_network, name="delete_swarm_network"),
    path("<uuid:network_id>/set_current/", views.set_current_network, name="set_current_network"),
    path("<uuid:network_id>/download/", views.download_startup_kits, name="download_startup_kits"),

]