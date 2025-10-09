from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from .utils import get_tailscale_ip, is_tailscale_connected, get_hostname, create_startup_kits_zip
from .models import SwarmNetwork, SwarmParticipant, UserCurrentNetwork
from apps.project.models import Project, UserCurrentProject
from .provision import generate_flare_startup_kit
from apps.logs.models import LogEntry
import os
import json
import yaml
import subprocess
from django.http import HttpResponse
from apps.logs.logger import get_logger

@login_required(login_url='/users/signin/')
def network(request):
    current_project_relation = UserCurrentProject.objects.get(user=request.user)
    swarm_networks = SwarmNetwork.objects.filter(project=current_project_relation.project)
    
    try:
        current_network = UserCurrentNetwork.objects.get(user=request.user).network
    except UserCurrentNetwork.DoesNotExist:
        current_network = None

    #! adapt later to show more detail also for uloaded startup kits (may include project.yml in startupkit download)
    participants_details = []
    if current_network:
        project_yml_path = os.path.join('workspaces', str(current_network.project.identifier), str(current_network.identifier), 'project.yml')
        if os.path.exists(project_yml_path):
            with open(project_yml_path, 'r') as f:
                project_yml = yaml.safe_load(f)
                for participant in project_yml.get('participants', []):
                    participants_details.append({
                        'name': participant.get('name'),
                        'org': participant.get('org'),
                        'ip': participant.get('ip'),
                    })

    context = {
        'segment': 'network',
        'tailscale_status': is_tailscale_connected(),
        'tailscale_ip': get_tailscale_ip(),
        'hostname': get_hostname(),
        'swarm_networks': swarm_networks,
        'current_network': current_network,
        'participants_details': participants_details,
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
            if startup_package:
                #! adapt in the future
                provision_dir = os.path.join('workspaces', str(project.identifier), str(swarm_network.identifier))
                os.makedirs(provision_dir, exist_ok=True)

                with zipfile.ZipFile(startup_package, 'r') as zip_ref:
                    zip_ref.extractall(provision_dir)

                swarm_network.status = 'PROVISIONED'
                swarm_network.save()

        return redirect('network')

    context = {
        'segment': 'network',
    }
    return render(request, "apps/network/new_network.html", context)

@login_required(login_url='/users/signin/')
def set_current_network(request, network_id):
    network = SwarmNetwork.objects.get(identifier=network_id)
    current_project_relation = UserCurrentProject.objects.get(user=request.user)
    if network.project == current_project_relation.project:
        current_network, created = UserCurrentNetwork.objects.get_or_create(user=request.user)
        current_network.network = network
        current_network.save()
    return redirect('network')

@login_required(login_url='/users/signin/')
def download_startup_kits(request, network_id):
    swarm_network = SwarmNetwork.objects.get(identifier=network_id)
    zip_buffer = create_startup_kits_zip(swarm_network)
    response = HttpResponse(zip_buffer.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = f'attachment; filename="{swarm_network.name}_startup_kits.zip"'
    return response

@login_required(login_url='/users/signin/')
def start_swarm_network(request, network_id):
    swarm_network = SwarmNetwork.objects.get(identifier=network_id)
    logger = get_logger(request.user, swarm_network.project)
    project_name = swarm_network.project.title.replace(' ', '_')
    provision_dir = os.path.join('workspaces', str(swarm_network.project.identifier), str(swarm_network.identifier))
    compose_dir = os.path.join(provision_dir, 'workspace', project_name, 'prod_00')
    compose_file_path = os.path.join(compose_dir, 'compose.yaml')

    logger.network.info(f"Looking for compose file at: {compose_file_path}")
    if os.path.exists(compose_file_path):
        host_project_path = os.getenv('HOST_PROJECT_PATH')
        if host_project_path:
            with open(compose_file_path, 'r') as f:
                compose_content = f.read()

            compose_content = compose_content.replace('build: ./nvflare', f'build: {os.path.join(host_project_path, compose_dir, "nvflare")}')
            compose_content = compose_content.replace('./fl-client', os.path.join(host_project_path, compose_dir, 'fl-client'))
            compose_content = compose_content.replace('./server', os.path.join(host_project_path, compose_dir, 'server'))
            compose_content = compose_content.replace('./overseer', os.path.join(host_project_path, compose_dir, 'overseer'))

            with open(compose_file_path, 'w') as f:
                f.write(compose_content)
            
            logger.network.info("Modified compose file to use absolute host paths.")

        logger.network.info("Compose file found. Running docker compose build")
        build_result = subprocess.run(['docker', 'compose', '-f', 'compose.yaml', 'build'], cwd=compose_dir, capture_output=True, text=True)
        logger.network.info(f"docker compose build stdout: {build_result.stdout}")
        logger.network.error(f"docker compose build stderr: {build_result.stderr}")

        logger.network.info("Running docker compose up -d")
        up_result = subprocess.run(['docker', 'compose', '-f', 'compose.yaml', 'up', '-d'], cwd=compose_dir, capture_output=True, text=True)
        logger.network.info(f"docker compose up stdout: {up_result.stdout}")
        logger.network.error(f"docker compose up stderr: {up_result.stderr}")
        swarm_network.status = 'RUNNING'
        swarm_network.save()
    else:
        logger.network.error(f"Compose file not found at: {compose_file_path}")

    return redirect('network')

@login_required(login_url='/users/signin/')
def stop_swarm_network(request, network_id):
    swarm_network = SwarmNetwork.objects.get(identifier=network_id)
    logger = get_logger(request.user, swarm_network.project)
    project_name = swarm_network.project.title.replace(' ', '_')
    provision_dir = os.path.join('workspaces', str(swarm_network.project.identifier), str(swarm_network.identifier))
    compose_dir = os.path.join(provision_dir, 'workspace', project_name, 'prod_00')
    compose_file_path = os.path.join(compose_dir, 'compose.yaml')

    if os.path.exists(compose_file_path):
        host_project_path = os.getenv('HOST_PROJECT_PATH')
        if host_project_path:
            with open(compose_file_path, 'r') as f:
                compose_content = f.read()

            compose_content = compose_content.replace('build: ./nvflare', f'build: {os.path.join(host_project_path, compose_dir, "nvflare")}')
            compose_content = compose_content.replace('./fl-client', os.path.join(host_project_path, compose_dir, 'fl-client'))
            compose_content = compose_content.replace('./server', os.path.join(host_project_path, compose_dir, 'server'))
            compose_content = compose_content.replace('./overseer', os.path.join(host_project_path, compose_dir, 'overseer'))

            with open(compose_file_path, 'w') as f:
                f.write(compose_content)

        logger.network.info(f"Stopping swarm network {swarm_network.name}")
        subprocess.run(['docker-compose', '-f', 'compose.yaml', 'down'], cwd=compose_dir)
        swarm_network.status = 'STOPPED'
        swarm_network.save()
        logger.network.info(f"Swarm network {swarm_network.name} stopped successfully")

    return redirect('network')

@require_POST
@login_required(login_url='/users/signin/')
def delete_swarm_network(request, network_id):
    swarm_network = SwarmNetwork.objects.get(identifier=network_id)
    if swarm_network.project.author == request.user:
        swarm_network.delete()
    return redirect('network')