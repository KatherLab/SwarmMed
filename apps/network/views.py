from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from .utils import get_tailscale_ip, is_tailscale_connected
from .models import SwarmNetwork, SwarmParticipant
from apps.project.models import Project, UserCurrentProject
from .provision import generate_flare_startup_kit
from .tasks import execute_and_log_in_container
from apps.logs.models import LogEntry
import docker
import os

@login_required(login_url='/users/signin/')
def network(request):
    current_project_relation = UserCurrentProject.objects.get(user=request.user)
    swarm_networks = SwarmNetwork.objects.filter(project=current_project_relation.project)

    context = {
        'segment': 'network',
        'tailscale_status': is_tailscale_connected(),
        'tailscale_ip': get_tailscale_ip(),
        'swarm_networks': swarm_networks,
    }
    return render(request, "apps/network/network.html", context)

@login_required(login_url='/users/signin/')
def new_network(request):
    if request.method == 'POST':
        network_name = request.POST.get('title')
        current_project_relation = UserCurrentProject.objects.get(user=request.user)
        project = current_project_relation.project

        # Create the Swarm Network
        swarm_network = SwarmNetwork.objects.create(
            name=network_name,
            project=project,
        )

        # Add the server participant (placeholder)
        SwarmParticipant.objects.create(
            network=swarm_network,
            user=request.user, # Or a dedicated server user
            role='SERVER',
            participant_id='server'
        )

        # Add the local client participant
        SwarmParticipant.objects.create(
            network=swarm_network,
            user=request.user,
            role='CLIENT',
            participant_id='fl-client'
        )
        
        # Trigger provisioning as a background task
        # For now, we run it directly for simplicity
        generate_flare_startup_kit(swarm_network.identifier)

        return redirect('network_detail', network_id=swarm_network.identifier)

    context = {
        'segment': 'network',
    }
    return render(request, "apps/network/new_network.html", context)

@login_required(login_url='/users/signin/')
def network_detail(request, network_id):
    swarm_network = SwarmNetwork.objects.get(identifier=network_id)
    network_logs = LogEntry.objects.filter(swarm_network=swarm_network).order_by('-timestamp')[:100]
    context = {
        'segment': 'network',
        'network': swarm_network,
        'logs': network_logs,
    }
    return render(request, "apps/network/network_detail.html", context)

@login_required(login_url='/users/signin/')
def start_swarm_network(request, network_id):
    swarm_network = SwarmNetwork.objects.get(identifier=network_id)

    project_name = swarm_network.project.title.replace(' ', '_')
    base_prod_path = os.path.join('workspaces', str(swarm_network.project.identifier), 'provision', str(swarm_network.identifier), 'workspace', project_name, 'prod_00')

    overseer_kit_path = os.path.join(base_prod_path, 'server', 'startup')
    client_kit_path = os.path.join(base_prod_path, 'fl-client', 'startup')

    # Start Overseer
    overseer_command = f"/bin/bash -c 'cd /app/{overseer_kit_path} && ./start.sh'"
    execute_and_log_in_container.delay(
        'overseer', 
        overseer_command, 
        str(swarm_network.identifier), 
        str(swarm_network.project.identifier), 
        request.user.id
    )

    # Start Client
    client_command = f"/bin/bash -c 'cd /app/{client_kit_path} && ./start.sh'"
    execute_and_log_in_container.delay(
        'fl-client', 
        client_command, 
        str(swarm_network.identifier), 
        str(swarm_network.project.identifier), 
        request.user.id
    )

    swarm_network.status = 'RUNNING'
    swarm_network.save()

    return redirect('network_detail', network_id=swarm_network.identifier)

@require_POST
@login_required(login_url='/users/signin/')
def delete_swarm_network(request, network_id):
    swarm_network = SwarmNetwork.objects.get(identifier=network_id)
    if swarm_network.project.author == request.user:
        swarm_network.delete()
    return redirect('network')