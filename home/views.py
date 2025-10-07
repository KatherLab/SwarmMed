from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.db import models
from apps.project.models import Project, UserCurrentProject
from apps.data.utils import get_storage_stats, format_size
from apps.network.models import SwarmNetwork, UserCurrentNetwork, SwarmParticipant

def get_user_project_uuid(request):
    """
    Get the current user's active project UUID.
    
    Args:
        request: Django request object
        
    Returns:
        tuple: (project_uuid, is_valid)
    """
    try:
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        if not user_current_project.project:
            return None, False
        
        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False

@login_required(login_url='/users/signin/')
def index(request):
    user = request.user
    
    # Get projects where user is author or member
    user_projects = Project.objects.filter(
        models.Q(author=user) | models.Q(members=user)
    ).distinct()
    
    # Get current project for this user
    current_project = None
    try:
        current_project_relation = UserCurrentProject.objects.get(user=user)
        current_project = current_project_relation.project
    except UserCurrentProject.DoesNotExist:
        # If no current project set, use the most recent project
        current_project = user_projects.order_by('-created_at').first()
    
    # Calculate project statistics
    total_projects = user_projects.count()
    projects_as_author = user_projects.filter(author=user).count()
    projects_as_member = user_projects.filter(members=user).exclude(author=user).count()
    
    # Get user's display name
    user_display_name = user.get_full_name() or user.username
    
    # Get dynamic data statistics
    total_files = 0
    total_storage = "0 B"
    total_folders = 0
    
    if current_project:
        # Get the current project's data directory
        project_uuid = str(current_project.identifier)
        data_prefix = f"{project_uuid}/data/"
        
        try:
            # Get storage statistics for this project
            total_size_bytes, folder_count, file_count = get_storage_stats(data_prefix)
            total_files = file_count
            total_storage = format_size(total_size_bytes)
            total_folders = folder_count
        except Exception as e:
            # Fallback in case of S3 connection issues
            print(f"Error getting storage stats: {e}")
            total_files = 0
            total_storage = "0 B"
            total_folders = 0
    
    # Get aggregated data statistics across all user projects
    total_files_all_projects = 0
    total_storage_all_projects = 0
    total_folders_all_projects = 0
    
    for project in user_projects:
        project_uuid = str(project.identifier)
        data_prefix = f"{project_uuid}/data/"
        
        try:
            size_bytes, folder_count, file_count = get_storage_stats(data_prefix)
            total_files_all_projects += file_count
            total_storage_all_projects += size_bytes
            total_folders_all_projects += folder_count
        except Exception:
            # Skip projects with storage issues
            continue
    
    # Format aggregated storage
    formatted_total_storage_all = format_size(total_storage_all_projects)
    
    # Network data
    total_networks = SwarmNetwork.objects.filter(project__in=user_projects).count()
    current_network_obj = None
    network_partners = 0
    
    try:
        user_current_network = UserCurrentNetwork.objects.get(user=request.user)
        if user_current_network.network and user_current_network.network.project in user_projects:
            current_network_obj = user_current_network.network
    except UserCurrentNetwork.DoesNotExist:
        current_network_obj = SwarmNetwork.objects.filter(project__in=user_projects).order_by('-created_at').first()

    if current_network_obj:
        network_partners = SwarmParticipant.objects.filter(network=current_network_obj).count()
        current_network_name = current_network_obj.name
    else:
        current_network_name = "No active network"
    
    # Training progress (mock data - replace with actual training status)
    training_status = "Running..."
    training_progress = 50  # Percentage
    
    context = {
        'segment': 'dashboard',
        'user_display_name': user_display_name,
        'current_project': current_project,
        'total_projects': total_projects,
        'projects_as_author': projects_as_author,
        'projects_as_member': projects_as_member,
        
        # Current project data statistics
        'total_files': total_files,
        'total_storage': total_storage,
        'total_folders': total_folders,
        
        # All projects data statistics
        'total_files_all_projects': total_files_all_projects,
        'total_storage_all_projects': formatted_total_storage_all,
        'total_folders_all_projects': total_folders_all_projects,
        
        # Network data
        'total_networks': total_networks,
        'current_network': current_network_name,
        'network_partners': network_partners,
        
        # Training data (mock for now)
        'training_status': training_status,
        'training_progress': training_progress,
    }
    
    return render(request, "dashboard/index.html", context)

def starter(request):
    context = {}
    return render(request, "pages/starter.html", context)