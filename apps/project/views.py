import json
import os
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.db import models 
from django.core.files.storage import default_storage
from .models import Project, UserCurrentProject
from .forms import ProjectForm
import re
import uuid
from ..users.models import Profile  


@login_required(login_url='/users/signin/')
def project_list(request):
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
    
    # Count of projects
    projects_count = user_projects.count()
    
    return render(request, 'apps/project/project.html', {
        'projects': user_projects,
        'current_project': current_project,
        'projects_count': projects_count,
    })

@login_required(login_url='/users/signin/')
def project_create(request):
    if request.method == 'POST':
        form = ProjectForm(request.POST, request.FILES)
        if form.is_valid():
            project = form.save(commit=False)
            # Automatically set the current user as author
            project.author = request.user
            project.save()
            
            # Process member identifiers
            member_identifiers = form.cleaned_data.get('member_identifiers', '')
            if member_identifiers:
                # Split by commas or new lines
                identifiers = [id.strip() for id in re.split(r'[,\n]', member_identifiers) if id.strip()]
                
                for identifier in identifiers:
                    try:
                        # Try to convert string to UUID to validate format
                        uuid_obj = uuid.UUID(identifier)
                        # Find the user with this identifier
                        profile = Profile.objects.get(identifier=uuid_obj)
                        project.members.add(profile.user)
                    except (ValueError, Profile.DoesNotExist):
                        # Invalid UUID format or no user with this identifier
                        continue
            
            # Process training code files (multiple files)
            training_code_directories_json = request.POST.get('training_code_directories', '{}')
            if training_code_directories_json:
                directories = json.loads(training_code_directories_json)
                files = request.FILES.getlist('training_code')
                
                # Set the root path to be within the project's code directory
                root_path = f"{project.identifier}/code/training/"
                
                for idx, file in enumerate(files):
                    key = file.name + '_' + str(idx)
                    rel_path = directories.get(key, file.name)
                    save_path = os.path.join(root_path, rel_path).replace('\\', '/')  # Ensure forward slashes
                    default_storage.save(save_path, file)
            
            return redirect('project_list')
    else:
        form = ProjectForm()
    return render(request, 'apps/project/new_project.html', {'form': form})


@login_required(login_url='/users/signin/')
def project_edit(request, pk):
    project = get_object_or_404(Project, pk=pk)
    # Check if user is author or member of the project
    if request.user != project.author and request.user not in project.members.all():
        return redirect('project_list')
    
    if request.method == 'POST':
        form = ProjectForm(request.POST, request.FILES, instance=project)
        if form.is_valid():
            form.save()
            
            # Clear existing members
            project.members.clear()
            
            # Process member identifiers
            member_identifiers = form.cleaned_data.get('member_identifiers', '')
            if member_identifiers:
                # Split by commas or new lines
                identifiers = [id.strip() for id in re.split(r'[,\n]', member_identifiers) if id.strip()]
                
                for identifier in identifiers:
                    try:
                        uuid_obj = uuid.UUID(identifier)
                        profile = Profile.objects.get(identifier=uuid_obj)
                        project.members.add(profile.user)
                    except (ValueError, Profile.DoesNotExist):
                        continue
            
            # Process training code files (multiple files)
            training_code_directories_json = request.POST.get('training_code_directories', '{}')
            if training_code_directories_json:
                directories = json.loads(training_code_directories_json)
                files = request.FILES.getlist('training_code')
                
                # Only process if files were uploaded
                if files:
                    # Set the root path to be within the project's code directory
                    root_path = f"{project.identifier}/code/training/"
                    
                    for idx, file in enumerate(files):
                        key = file.name + '_' + str(idx)
                        rel_path = directories.get(key, file.name)
                        save_path = os.path.join(root_path, rel_path).replace('\\', '/')  # Ensure forward slashes
                        default_storage.save(save_path, file)
            
            return redirect('project_list')
    else:
        form = ProjectForm(instance=project)
    return render(request, 'apps/project/new_project.html', {'form': form, 'edit': True})


@login_required(login_url='/users/signin/')
def project_delete(request, pk):
    project = get_object_or_404(Project, pk=pk)
    # Only allow the author to delete the project
    if request.user == project.author:
        project.delete()
    return redirect('project_list')

@login_required(login_url='/users/signin/')
def set_current_project(request, pk):
    # Get project
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
