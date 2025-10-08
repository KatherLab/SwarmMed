import os
import shutil
import subprocess
import textwrap
from .models import SwarmNetwork


def generate_flare_startup_kit(network_id: str, local_test: bool = False, clients: list = []):
    """
    Generates the startup kits for a given SwarmNetwork.
    """
    try:
        network = SwarmNetwork.objects.get(identifier=network_id)
    except SwarmNetwork.DoesNotExist:
        print(f"Error: SwarmNetwork with id {network_id} not found.")
        return

    project_dir = os.path.join('workspaces', str(network.project.identifier))
    provision_dir = os.path.join(project_dir, str(network.identifier))

    if os.path.exists(provision_dir):
        shutil.rmtree(provision_dir)
    os.makedirs(provision_dir)

    participants = []
    if local_test:
        participants.append({
            'name': 'server',
            'type': 'server',
            'org': 'nvidia',
            'fed_learn_port': 8002,
            'admin_port': 8003,
        })
        participants.append({
            'name': 'fl-client',
            'type': 'client',
            'org': 'nvidia',
        })
    else:
        participants.append({
            'name': 'server',
            'type': 'server',
            'org': 'nvidia',
            'fed_learn_port': 8002,
            'admin_port': 8003,
        })
        for client in clients:
            participants.append({
                'name': client['name'],
                'type': 'client',
                'org': 'nvidia',
                'listening_host': client['ip'],
            })

    participants.append({
        'name': 'admin@nvidia.com',
        'type': 'admin',
        'org': 'nvidia',
        'role': 'project_admin',
    })

    participants_yaml = ""
    for p in participants:
        participants_yaml += f"      - name: {p['name']}\n"
        participants_yaml += f"        type: {p['type']}\n"
        participants_yaml += f"        org: {p['org']}\n"
        if 'fed_learn_port' in p:
            participants_yaml += f"        fed_learn_port: {p['fed_learn_port']}\n"
        if 'admin_port' in p:
            participants_yaml += f"        admin_port: {p['admin_port']}\n"
        if 'listening_host' in p:
            participants_yaml += f"        listening_host: {p['listening_host']}\n"
        if 'role' in p:
            participants_yaml += f"        role: {p['role']}\n"

    project_yml_content = textwrap.dedent(f"""
    api_version: 3
    name: {network.project.title.replace(' ', '_')}
    description: FLARE project for {network.project.title}

    participants:
{participants_yaml}
    # The same methods in all builders are called in their order defined in builders section
    builders:
      - path: nvflare.lighter.impl.workspace.WorkspaceBuilder
        args:
          template_file: master_template.yml
      - path: nvflare.lighter.impl.docker.DockerBuilder
        args:
          base_image: python:3.8
      - path: nvflare.lighter.impl.static_file.StaticFileBuilder
      - path: nvflare.lighter.impl.cert.CertBuilder
      - path: nvflare.lighter.impl.signature.SignatureBuilder
    """).strip()
    
    project_yml_path = os.path.join(provision_dir, 'project.yml')
    with open(project_yml_path, 'w') as f:
        f.write(project_yml_content)

    # Copy master template
    shutil.copy(os.path.join('apps', 'network', 'master_template.yml'), os.path.join(provision_dir, 'master_template.yml'))
    
    try:
        print(f"Starting provisioning for network {network_id} in {provision_dir}")
        command = [
            'nvflare',
            'provision',
            '-p',
            os.path.join(str(network.identifier), 'project.yml'),
            '-w',
            os.path.join(str(network.identifier), 'workspace')
        ]
        
        print(f"Running provisioning command: {' '.join(command)} in {project_dir}")
        result = subprocess.run(command, cwd=project_dir, capture_output=True, text=True, check=True)
        print(f"Provisioning stdout: {result.stdout}")
        print(f"Provisioning stderr: {result.stderr}")
        
        print(f"Successfully provisioned startup kit in {provision_dir}")
        network.status = 'PROVISIONED'
        network.save()

    except subprocess.CalledProcessError as e:
        print(f"An exception occurred during provisioning: {e}")
        print(f"Provisioning stdout: {e.stdout}")
        print(f"Provisioning stderr: {e.stderr}")
        network.status = 'ERROR'
        network.save()
    except Exception as e:
        print(f"An exception occurred during provisioning: {e}")
        network.status = 'ERROR'
        network.save()
