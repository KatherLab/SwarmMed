from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from .utils import get_tailscale_ip, is_tailscale_connected, get_hostname
from .models import SwarmNetwork, SwarmParticipant, UserCurrentNetwork
from apps.project.models import Project, UserCurrentProject
from .provision import generate_flare_startup_kit
from .tasks import execute_and_log_in_container
from apps.logs.models import LogEntry
import docker
import os
import json
import io
import zipfile
from django.http import HttpResponse

@login_required(login_url='/users/signin/')
def network(request):
    current_project_relation = UserCurrentProject.objects.get(user=request.user)
    swarm_networks = SwarmNetwork.objects.filter(project=current_project_relation.project)
    
    try:
        current_network = UserCurrentNetwork.objects.get(user=request.user).network
    except UserCurrentNetwork.DoesNotExist:
        current_network = None

    context = {
        'segment': 'network',
        'tailscale_status': is_tailscale_connected(),
        'tailscale_ip': get_tailscale_ip(),
        'hostname': get_hostname(),
        'swarm_networks': swarm_networks,
        'current_network': current_network,
    }
    return render(request, "apps/network/network.html", context)

@login_required(login_url='/users/signin/')
def new_network(request):
    if request.method == 'POST':
        creation_method = request.POST.get('creation_method')
        network_name = request.POST.get('title')
        description = request.POST.get('description')
        
        current_project_relation = UserCurrentProject.objects.get(user=request.user)
        project = current_project_relation.project

        swarm_network = SwarmNetwork.objects.create(
            name=network_name,
            project=project,
            description=description,
            author=request.user
        )

        if creation_method == 'create':
            local_test = request.POST.get('local_test') == 'on'
            clients_json = request.POST.getlist('clients')
            clients = [json.loads(c) for c in clients_json]

            # Add participants to the database
            SwarmParticipant.objects.create(
                network=swarm_network,
                user=request.user,
                role='SERVER',
                participant_id='server'
            )
            for client_data in clients:
                SwarmParticipant.objects.create(
                    network=swarm_network,
                    user=request.user,
                    role='CLIENT',
                    participant_id=client_data['name']
                )

            generate_flare_startup_kit(
                network_id=swarm_network.identifier,
                local_test=local_test,
                clients=clients
            )

        elif creation_method == 'upload':
            startup_package = request.FILES.get('startup_package')
            # TODO: Handle file upload
            pass

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
def set_current_network(request, network_id):
    network = SwarmNetwork.objects.get(identifier=network_id)
    current_network, created = UserCurrentNetwork.objects.get_or_create(user=request.user)
    current_network.network = network
    current_network.save()
    return redirect('network')

@login_required(login_url='/users/signin/')
def download_startup_kits(request, network_id):
    swarm_network = SwarmNetwork.objects.get(identifier=network_id)
    project_name = swarm_network.project.title.replace(' ', '_')
    base_prod_path = os.path.join('/app', 'workspaces', str(swarm_network.project.identifier), 
                                   str(swarm_network.identifier), 'workspace', project_name, 'prod_00')
    
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        # Iterate through directories
        for item in os.scandir(base_prod_path):
            if item.is_dir() and item.name != 'server' and 'admin' not in item.name:
                client_dir_path = item.path
                
                # Walk through all files in the client directory
                for root, dirs, files in os.walk(client_dir_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        
                        # Create archive name with the client directory as the top-level folder
                        # This preserves the directory structure inside the zip
                        arcname = os.path.join(item.name, os.path.relpath(file_path, client_dir_path))
                        
                        # Add file to zip with the directory structure
                        zf.write(file_path, arcname)
    
    # Seek to beginning before reading
    zip_buffer.seek(0)
    
    response = HttpResponse(zip_buffer.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{swarm_network.name}_startup_kits.zip"'
    return response


@login_required(login_url='/users/signin/')
def start_swarm_network(request, network_id):
    swarm_network = SwarmNetwork.objects.get(identifier=network_id)

    project_name = swarm_network.project.title.replace(' ', '_')
    base_prod_path = os.path.join('workspaces', str(swarm_network.project.identifier), str(swarm_network.identifier), 'workspace', project_name, 'prod_00')

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