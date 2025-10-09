import os
import shutil
import subprocess
import textwrap
import json
from pathlib import Path
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

    # Copy master_template.yml to provision_dir
    repo_template_path = Path(__file__).resolve().parent / 'master_template.yml'
    target_template_path = Path(provision_dir) / 'master_template.yml'
    if not repo_template_path.exists():
        print(f"ERROR: master_template.yml not found at {repo_template_path}")
        return
    shutil.copyfile(str(repo_template_path), str(target_template_path))
    print(f"Copied master_template.yml to {target_template_path}")

    with open(target_template_path, "r") as tf:
        print("--- master_template.yml (effective) ---")
        print(tf.read())
        print("--------------------------------------")
    
    abs_template_path = str(target_template_path.resolve())

    participants = []
    participants.append({
        'name': 'overseer',
        'type': 'overseer',
        'org': 'nvidia',
    })
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
          template_file: "{abs_template_path}"
      - path: nvflare.lighter.impl.docker.DockerBuilder
        args:
          base_image: python:3.10-slim
      - path: nvflare.lighter.impl.static_file.StaticFileBuilder
      - path: nvflare.lighter.impl.cert.CertBuilder
      - path: nvflare.lighter.impl.signature.SignatureBuilder
    """).strip()
    
    project_yml_path = os.path.join(provision_dir, 'project.yml')
    with open(project_yml_path, 'w') as f:
        f.write(project_yml_content)

    try:
        print("nvflare version used for provisioning:")
        subprocess.run(['python', '-c', 'import nvflare, sys; print(getattr(nvflare, "version", "unknown"), sys.executable)'], cwd=provision_dir)

        print(f"Starting provisioning for network {network_id} in {provision_dir}")
        command = [
            'nvflare',
            'provision',
            '-p',
            'project.yml',
            '-w',
            'workspace'
        ]
        
        print(f"Running provisioning command: {' '.join(command)} in {provision_dir}")
        result = subprocess.run(command, cwd=provision_dir, capture_output=True, text=True, check=True)
        print(f"Provisioning stdout: {result.stdout}")
        print(f"Provisioning stderr: {result.stderr}")
        
        print(f"Successfully provisioned startup kit in {provision_dir}")
        network.status = 'PROVISIONED'
        network.save()

        # Print the content of fed_server.json for debugging
        try:
            project_name = network.project.title.replace(' ', '_')
            fed_server_json_path = os.path.join(provision_dir, 'workspace', project_name, 'prod_00', 'server', 'startup', 'fed_server.json')
            if os.path.exists(fed_server_json_path):
                with open(fed_server_json_path, 'r') as f:
                    print("--- fed_server.json content ---")
                    fs = json.load(f)
                    
                    # Find the server component and update its sp_end_point
                    for component in fs.get('servers', []):
                        if component.get('name') == 'server':
                            component['sp_end_point'] = 'host.docker.internal:8002'
                            print("Updated server sp_end_point to host.docker.internal:8002")
                            break
                    
                    # Write the updated content back to the file
                    with open(fed_server_json_path, 'w') as f_write:
                        json.dump(fs, f_write, indent=2)

                    print(json.dumps(fs, indent=2))
                    print("-----------------------------")
            else:
                print(f"!!! fed_server.json not found at {fed_server_json_path}")
        except Exception as e:
            print(f"Could not print or patch fed_server.json content: {e}")

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
