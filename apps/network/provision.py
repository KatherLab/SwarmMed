import os
import shutil
import subprocess
import textwrap
from .models import SwarmNetwork


def generate_flare_startup_kit(network_id: str):
    """
    Generates the startup kits for a given SwarmNetwork.
    """
    try:
        network = SwarmNetwork.objects.get(identifier=network_id)
    except SwarmNetwork.DoesNotExist:
        print(f"Error: SwarmNetwork with id {network_id} not found.")
        return

    project_dir = os.path.join('workspaces', str(network.project.identifier))
    provision_dir = os.path.join(project_dir, 'provision', str(network.identifier))

    if os.path.exists(provision_dir):
        shutil.rmtree(provision_dir)
    os.makedirs(provision_dir)

    project_yml_content = textwrap.dedent(f"""
    api_version: 3
    name: {network.project.title.replace(' ', '_')}
    description: FLARE project for {network.project.title}

    participants:
      # Change the name of the server (server1) to the Fully Qualified Domain Name
      # (FQDN) of the server, for example: server1.example.com.
      # Ensure that the FQDN is correctly mapped in the /etc/hosts file.
      - name: server
        type: server
        org: nvidia
        fed_learn_port: 8002
        admin_port: 8003
    #    docker_comm_port: 8005
      - name: fl-client
        type: client
        org: nvidia
        # Specifying listening_host will enable the creation of one pair of
        # certificate/private key for this client, allowing the client to function
        # as a server for 3rd-party integration.
        # The value must be a hostname that the external trainer can reach via the network.
        # listening_host: site-1-lh
    #    docker_comm_port: 8006
      - name: admin@nvidia.com
        type: admin
        org: nvidia
        role: project_admin

    # The same methods in all builders are called in their order defined in builders section
    builders:
      - path: nvflare.lighter.impl.workspace.WorkspaceBuilder
        args:
          template_file: master_template.yml
      - path: nvflare.lighter.impl.static_file.StaticFileBuilder
        args:
          # config_folder can be set to inform NVIDIA FLARE where to get configuration
          config_folder: config

          # scheme for communication driver (currently supporting the default, grpc, only).
          # scheme: grpc

          # app_validator is used to verify if uploaded app has proper structures
          # if not set, no app_validator is included in fed_server.json
          # app_validator: PATH_TO_YOUR_OWN_APP_VALIDATOR

          # when docker_image is set to a docker image name, docker.sh will be generated on server/client/admin
          # docker_image:

          # download_job_url is set to http://download.server.com/ as default in fed_server.json.  You can override this
          # to different url.
          # download_job_url: http://download.server.com/
    #      docker_image: localhost/nvflare:0.0.1
    #
    #  - path: nvflare.lighter.impl.docker.DockerBuilder
    #    args:
    #      docker_image: localhost/nvflare:0.0.1
    #      base_image: python:3.10
    #      requirements_file: docker_compose_requirements.txt
      - path: nvflare.lighter.impl.cert.CertBuilder
      - path: nvflare.lighter.impl.signature.SignatureBuilder
    """).strip()
    
    project_yml_path = os.path.join(provision_dir, 'project.yml')
    with open(project_yml_path, 'w') as f:
        f.write(project_yml_content)

    # Copy master template
    shutil.copy(os.path.join('apps', 'network', 'master_template.yml'), os.path.join(provision_dir, 'master_template.yml'))
    
    # Copy the config and custom directories
    shutil.copytree(os.path.join('nvflare', 'config'), os.path.join(provision_dir, 'config'))
    shutil.copytree(os.path.join('nvflare', 'custom'), os.path.join(provision_dir, 'custom'))

    try:
        print(f"Starting provisioning for network {network_id} in {provision_dir}")
        command = [
            'nvflare',
            'provision',
            '-p',
            'project.yml'
        ]
        
        print(f"Running provisioning command: {' '.join(command)} in {provision_dir}")
        # Let the subprocess stream its output directly to the container logs
        subprocess.run(command, cwd=provision_dir, check=True)
        
        print(f"Successfully provisioned startup kit in {provision_dir}")
        network.status = 'PROVISIONED'
        network.save()

    except Exception as e:
        print(f"An exception occurred during provisioning: {e}")
        network.status = 'ERROR'
        network.save()
