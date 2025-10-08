import subprocess
from django.core.cache import cache
import socket
import io
import zipfile
import os
from .tasks import execute_and_log_in_container

def get_tailscale_ip():
    """Get the current machine's Tailscale IPv4 address."""
    cached_ip = cache.get('tailscale_ip')
    if cached_ip:
        return cached_ip
    
    try:
        result = subprocess.run(['tailscale', 'ip', '--4'], 
                              capture_output=True, 
                              text=True, 
                              check=True)
        ip = result.stdout.strip()
        cache.set('tailscale_ip', ip, 300)  # Cache for 5 minutes
        return ip
    except PermissionError:
        return "Permission Denied"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "Not Available"

def is_tailscale_connected():
    """Check if Tailscale is currently connected."""
    cached_state = cache.get('tailscale_connected')
    if cached_state is not None:
        return cached_state
    
    try:
        result = subprocess.run(['tailscale', 'status'], 
                              capture_output=True, 
                              text=True, 
                              check=True)
        
        is_connected = bool(result.stdout.strip()) and 'peerapi' not in result.stdout.lower()
        
        status = "connected" if is_connected else "disconnected"
        cache.set('tailscale_connected', status, 30)
        return status

    except PermissionError:
        cache.set('tailscale_connected', "Permission Denied", 10)
        return "Permission Denied"
    except (subprocess.CalledProcessError, FileNotFoundError):
        cache.set('tailscale_connected', "disconnected", 10)
        return "disconnected"

def get_hostname():
    """Get the current machine's hostname."""
    try:
        return socket.gethostname()
    except Exception:
        return "Not Available"

def create_startup_kits_zip(swarm_network):
    project_name = swarm_network.project.title.replace(' ', '_')
    base_prod_path = os.path.join('/app', 'workspaces', str(swarm_network.project.identifier), 
                                   str(swarm_network.identifier), 'workspace', project_name, 'prod_00')
    
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        # Iterate through directories
        for item in os.scandir(base_prod_path):
            if item.is_dir() and item.name != 'server' and 'admin' not in item.name:
                client_dir_path = item.path
                
                client_zip_buffer = io.BytesIO()
                with zipfile.ZipFile(client_zip_buffer, 'w', compression=zipfile.ZIP_DEFLATED) as client_zf:
                    for root, _, files in os.walk(client_dir_path):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, client_dir_path)
                            client_zf.write(file_path, arcname)
                
                zip_data = client_zip_buffer.getvalue()
                if zip_data:
                    zf.writestr(f"{item.name}.zip", zip_data)

    zip_buffer.seek(0)
    return zip_buffer

def start_network_containers(swarm_network, user_id):
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
        user_id
    )

    # Start Client
    client_command = f"/bin/bash -c 'cd /app/{client_kit_path} && ./start.sh'"
    execute_and_log_in_container.delay(
        'fl-client', 
        client_command, 
        str(swarm_network.identifier), 
        str(swarm_network.project.identifier), 
        user_id
    )

def stop_network_containers(swarm_network, user_id):
    # Stop Overseer
    execute_and_log_in_container.delay(
        'overseer', 
        'docker stop overseer', 
        str(swarm_network.identifier), 
        str(swarm_network.project.identifier), 
        user_id
    )

    # Stop Client
    execute_and_log_in_container.delay(
        'fl-client', 
        'docker stop fl-client', 
        str(swarm_network.identifier), 
        str(swarm_network.project.identifier), 
        user_id
    )