from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from .models import Project
from .forms import ProjectForm

@login_required(login_url='/users/signin/')
def project_list(request):
    projects = Project.objects.all().order_by('-creation_date')
    current_project = Project.objects.filter(is_current=True).first()
    # Count of projects
    projects_count = projects.count()
    # You might need to add logic for finished projects if that's a status in your model
    
    return render(request, 'apps/project/project.html', {
        'projects': projects,
        'current_project': current_project,
        'projects_count': projects_count,
    })

def project_create(request):
    if request.method == 'POST':
        form = ProjectForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            return redirect('project_list')
    else:
        form = ProjectForm()
    return render(request, 'apps/project/new_project.html', {'form': form})

def project_edit(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if request.method == 'POST':
        form = ProjectForm(request.POST, request.FILES, instance=project)
        if form.is_valid():
            form.save()
            return redirect('project_list')
    else:
        form = ProjectForm(instance=project)
    return render(request, 'apps/project/new_project.html', {'form': form, 'edit': True})

def project_delete(request, pk):
    project = get_object_or_404(Project, pk=pk)
    project.delete()
    return redirect('project_list')

def set_current_project(request, pk):
    Project.objects.update(is_current=False)
    project = get_object_or_404(Project, pk=pk)
    project.is_current = True
    project.save()
    return redirect('project_list')

