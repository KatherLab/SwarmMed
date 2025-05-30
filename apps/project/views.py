import json
import os
import shutil
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.db import models 
from django.core.files.storage import default_storage
from django.conf import settings
from .models import Project, UserCurrentProject
from .forms import ProjectForm
from .utils import process_member_identifiers, handle_training_code_upload
from apps.logs import logger

@login_required(login_url='/users/signin/')
def project_list(request):
    """List all projects where the user is author or member."""
    # Get only projects where the user is author or member
    user_projects = Project.objects.filter(
        models.Q(author=request.user) | models.Q(members=request.user)
    ).distinct().order_by('-creation_date')
    
    # Get current project for this specific user
    try:
        current_project_relation = UserCurrentProject.objects.get(user=request.user)
        current_project = current_project_relation.project
    except UserCurrentProject.DoesNotExist:
        current_project = None
    
    # Count of finished projects (assuming a status field or other criteria)
    finished_projects_count = 0  # Replace with actual query when implemented
    
    context = {
        'segment': 'project',
        'projects': user_projects,
        'current_project': current_project,
        'finished_projects_count': finished_projects_count,
    }
    
    return render(request, 'apps/project/project.html', context)

@login_required(login_url='/users/signin/')
def project_create(request):
    """Create a new project."""
    log = logger.get_logger(user=request.user, project=None)
     
    if request.method == 'POST':
        form = ProjectForm(request.POST, request.FILES)
        if form.is_valid():
            project = form.save(commit=False)
            project.author = request.user
            
            # Save the project first to get an ID
            project.save()
            
           
            
            # Process member identifiers
            process_member_identifiers(project, form.cleaned_data.get('member_identifiers', ''))
            
            # Process training code files
            try:
                handle_training_code_upload(project, request)
            except Exception as e:
                log.project.error(f"ERROR PROCESSING TRAINING CODE UPLOAD - {project.title}: {str(e)}")

            log.project.info(f"PROJECT CREATED SUCCESSFULLY - {project.title}")

            return redirect('project_list')
    else:
        form = ProjectForm()
    
    context = {
        'segment': 'project',
        'form': form,
    }
    return render(request, 'apps/project/new_project.html', context)

@login_required(login_url='/users/signin/')
def project_edit(request, pk):
    """Edit an existing project."""
    log = logger.get_logger()
    project = get_object_or_404(Project, pk=pk)
    if request.user != project.author and request.user not in project.members.all():
        return redirect('project_list')
    
    if request.method == 'POST':
        form = ProjectForm(request.POST, request.FILES, instance=project)
        if form.is_valid():
            project = form.save(commit=False)
            project.save()
            
            # Process member identifiers
            process_member_identifiers(project, form.cleaned_data.get('member_identifiers', ''))
            
            # Process training code files
            handle_training_code_upload(project, request)
            
            # Remove current project settings for users who are no longer members
            UserCurrentProject.objects.filter(
                project=project
            ).exclude(
                user=project.author  # Author always has access
            ).exclude(
                user__in=project.members.all()  # Current members have access
            ).delete()
            
            log.project.info("Project updated successfully")
            
            return redirect('project_list')
    else:
        form = ProjectForm(instance=project)
    
    context = {
        'segment': 'project',
        'form': form,
        'edit': True,
    }
    return render(request, 'apps/project/new_project.html', context)

@login_required(login_url='/users/signin/')
def project_delete(request, pk):
    """Delete a project (author only)."""
    log = logger.get_logger(user=request.user, project=None)
    project = get_object_or_404(Project, pk=pk)
    # Only allow the author to delete the project
    if request.user == project.author:
        project.delete()
        log.project.info(f"Project deleted successfully - {project.title}")
    return redirect('project_list')

@login_required(login_url='/users/signin/')
def set_current_project(request, pk):
    """Set a project as the current project for a user."""
    project = get_object_or_404(Project, pk=pk)
    
    # Check if user has access to this project
    if not (project.author == request.user or request.user in project.members.all()):
        return redirect('project_list')
    
    # Update or create the user's current project
    UserCurrentProject.objects.update_or_create(
        user=request.user,
        defaults={'project': project}
    )
    
    return redirect('project_list')
