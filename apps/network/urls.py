"""
URL configuration for the network app.
Maps web addresses to the view functions for managing swarm networks.
"""

from django.urls import path
from . import views

# Standard application namespace
app_name = "network"

urlpatterns = [
    # Main dashboard showing all networks for the current project
    path("", views.network, name="network"),
    # Form to create a new network configuration
    path("new/", views.new_network, name="new_network"),
    # Action: Start the docker-compose deployment for a network
    path(
        "<uuid:network_id>/start/",
        views.start_swarm_network,
        name="start_swarm_network",
    ),
    # Action: Stop and remove the docker-compose deployment
    path(
        "<uuid:network_id>/stop/", views.stop_swarm_network, name="stop_swarm_network"
    ),
    # Action: Delete the network configuration and files
    path(
        "<uuid:network_id>/delete/",
        views.delete_swarm_network,
        name="delete_swarm_network",
    ),
    # Action: Set this network as the 'active' one for the current user
    path(
        "<uuid:network_id>/set_current/",
        views.set_current_network,
        name="set_current_network",
    ),
    # Action: Download the generated FLARE startup kits for clients
    path(
        "<uuid:network_id>/download/",
        views.download_startup_kits,
        name="download_startup_kits",
    ),
    # API: Get the current status of a specific network as JSON
    path(
        "<uuid:network_id>/status/",
        views.get_swarm_network_status,
        name="get_swarm_network_status",
    ),
]
