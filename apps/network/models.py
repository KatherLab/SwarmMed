from django.db import models
from django.contrib.auth.models import User
from apps.project.models import Project
import uuid
import os
import shutil
import subprocess
from django.conf import settings

class SwarmNetwork(models.Model):
    """
    Represents a Swarm Learning Network configuration.
    """
    STATUS_CHOICES = [
        ('INITIALIZING', 'Initializing'),
        ('PROVISIONED', 'Provisioned'),
        ('STARTING', 'Starting'),
        ('RUNNING', 'Running'),
        ('STOPPING', 'Stopping'),
        ('STOPPED', 'Stopped'),
        ('ERROR', 'Error'),
    ]

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='swarm_networks')
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='authored_swarm_networks', null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='INITIALIZING')
    identifier = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} for Project {self.project.title}"

    def delete(self, *args, **kwargs):
        if self.status == 'RUNNING':
            project_name = self.project.title.replace(' ', '_')
            provision_dir = os.path.join(settings.BASE_DIR, 'workspaces', str(self.project.identifier), str(self.identifier))
            compose_dir = os.path.join(provision_dir, 'workspace', project_name, 'prod_00')
            compose_file_path = os.path.join(compose_dir, 'compose.yaml')

            if os.path.exists(compose_file_path):
                host_project_path = os.getenv('HOST_PROJECT_PATH')
                if host_project_path:
                    with open(compose_file_path, 'r') as f:
                        compose_content = f.read()

                    relative_compose_dir = os.path.relpath(compose_dir, settings.BASE_DIR)
                    host_compose_dir = os.path.join(host_project_path, relative_compose_dir)

                    compose_content = compose_content.replace('build: ./nvflare', f'build: {os.path.join(host_compose_dir, "nvflare")}')
                    compose_content = compose_content.replace('./fl-client', os.path.join(host_compose_dir, 'fl-client'))
                    compose_content = compose_content.replace('./server', os.path.join(host_compose_dir, 'server'))
                    compose_content = compose_content.replace('./overseer', os.path.join(host_compose_dir, 'overseer'))

                    with open(compose_file_path, 'w') as f:
                        f.write(compose_content)

                subprocess.run(['docker-compose', '-f', 'compose.yaml', 'down'], cwd=compose_dir)

        # Clean up the provisioning directory before deleting the object
        provision_dir = os.path.join('workspaces', str(self.project.identifier), str(self.identifier))
        if os.path.exists(provision_dir):
            shutil.rmtree(provision_dir)
            print(f"Deleted provisioning directory: {provision_dir}")
        super().delete(*args, **kwargs)

class SwarmParticipant(models.Model):
    """
    Represents a participant (server or client) in a Swarm Learning Network.
    """
    ROLE_CHOICES = [
        ('SERVER', 'Server'),
        ('CLIENT', 'Client'),
    ]

    network = models.ForeignKey(SwarmNetwork, on_delete=models.CASCADE, related_name='participants')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='swarm_participations')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    participant_id = models.CharField(max_length=100, help_text="Unique identifier used by FLARE (e.g., 'server', 'client-1')")

    class Meta:
        unique_together = ('network', 'participant_id')

    def __str__(self):
        return f"{self.user.username} as {self.get_role_display()} in {self.network.name}"

class UserCurrentNetwork(models.Model):
    """Model to track which network is currently active for a user"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='current_network_relation')
    network = models.ForeignKey(SwarmNetwork, on_delete=models.SET_NULL, null=True, related_name='current_for_users')
    
    class Meta:
        unique_together = ('user', 'network')