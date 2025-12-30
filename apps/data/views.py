import json
import os
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.urls import reverse
from django.utils import timezone
from celery import current_app
from ..project.models import UserCurrentProject
from .models import ValidationRun, ValidationCheck, VisualizationRun, VisualizationPlot
from .tasks import run_validation_task, run_visualization_task
from .utils import (
    list_s3_folder, delete_s3_object, rename_s3_object, get_s3_download_url,
    delete_s3_folder, rename_s3_folder, get_storage_stats, format_size,
    get_column_prefixes
)
from apps.logs import logger

def get_user_project(request):
    """
    Get the current user's active project identifier.
    
    Args:
        request: Django request object
        
    Returns:
        tuple: (project_uuid, is_valid)
            - project_uuid: String UUID of the project or None
            - is_valid: Boolean indicating if a valid project was found
    """
    try:
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        if not user_current_project.project:
            return None, False
        
        return str(user_current_project.project.identifier), True
    except UserCurrentProject.DoesNotExist:
        return None, False

@login_required(login_url='/users/signin/')
def data(request):
    """
    View for the main data page showing storage statistics.
    """
    # Get the current user's active project
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return render(request, "apps/data/no_project_selected.html", {"segment": "data"})
    
    from ..project.models import Project
    project = get_object_or_404(Project, identifier=current_project_uuid)

    # Set the root path for the project's data directory
    root_path = f"{current_project_uuid}/data/"
    
    # Get storage statistics for this project
    total_size, folder_count, file_count = get_storage_stats(root_path)
    formatted_size = format_size(total_size)
    
    context = {
        'segment': 'data',
        'project': project,
        'folder_count': folder_count,
        'file_count': file_count,
        'space_used': formatted_size,
    }
    return render(request, "apps/data/data.html", context)

@login_required(login_url='/users/signin/')
def upload_files(request):
    """
    View for uploading files and folders.
    """
    # Get the current user's active project
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return render(request, "apps/data/no_project_selected.html", {"segment": "data"})

    # Initialize logger
    log = logger.get_logger()

    if request.method == 'POST':
        files = request.FILES.getlist('file_field')
        directories_json = request.POST.get('directories', '{}')
        directories = json.loads(directories_json)
        destination_folder = request.POST.get('destination_folder', '')
        
        # Set the root path to be within the project's data directory
        root_path = f"{current_project_uuid}/data/"
        
        # If destination_folder was provided, add it after the root_path
        if destination_folder:
            if not destination_folder.endswith('/'):
                destination_folder += '/'
            full_destination = f"{root_path}{destination_folder}"
        else:
            full_destination = root_path
            
        for idx, file in enumerate(files):
            key = file.name + '_' + str(idx)
            rel_path = directories.get(key, file.name)
            # Ensure forward slashes for consistency
            save_path = os.path.join(full_destination, rel_path).replace('\\', '/')
            default_storage.save(save_path, file)

        log.data.info(f"Files uploaded to {full_destination} successfully.")
        return HttpResponse('Files uploaded with folder structure preserved!')
    
    context = {
        'segment': 'data',
    }
    
    return render(request, 'apps/data/upload.html', context)

@login_required(login_url='/users/signin/')
def list_files(request):
    """
    View for listing files and folders in a column layout.
    """
    # Get the current user's active project
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return render(request, "apps/data/no_project_selected.html", {"segment": "data"})

    # Set the root path to be within the project's data directory
    root_path = f"{current_project_uuid}/data/"
    
    # Get the current folder from query params, default to root of the project
    user_prefix = request.GET.get('prefix', '')
    
    # Combine the root path with any additional path from query parameters
    full_prefix = root_path
    if user_prefix:
        full_prefix = f"{root_path}{user_prefix}"
    
    # Get column prefixes relative to the user's view
    column_prefixes = get_column_prefixes(user_prefix)
    columns = []
    
    for col_prefix in column_prefixes:
        # Convert the relative column prefix to full S3 path within project
        full_col_prefix = root_path
        if col_prefix:
            full_col_prefix = f"{root_path}{col_prefix}"
        
        # Get folders and files at this prefix
        folders, files = list_s3_folder(full_col_prefix)
        
        # Process folders - show only folders within this project
        processed_folders = []
        for folder in folders:
            # Skip folders not in this project
            if not folder.startswith(root_path):
                continue
                
            # For display: just show the folder name
            folder_name = folder[len(full_col_prefix):-1]
            
            # For navigation: use the path relative to the project
            relative_folder_path = folder[len(root_path):]
            
            processed_folders.append({
                'name': folder_name,
                'key': relative_folder_path,  # for navigation
                'full_key': folder            # for S3 operations
            })

        
        # Process files - show only files within this project
        processed_files = []
        for file in files:
            # Skip files not in this project
            if not file.startswith(root_path):
                continue
                
            # For display: just show the file name
            file_name = file[len(full_col_prefix):]
            
            processed_files.append({
                'name': file_name,
                'key': file,  # Keep full path for operations
                'download_url': get_s3_download_url(file)
            })
        
        columns.append({
            'prefix': col_prefix,
            'folders': processed_folders,
            'files': processed_files
        })
    
    # Build active_prefixes for highlighting
    active_prefixes = set()
    if user_prefix:
        parts = user_prefix.rstrip('/').split('/')
        for i in range(len(parts)):
            active_prefixes.add('/'.join(parts[:i+1]) + '/')
    
    context = {
        'segment': 'data',
        'columns': columns,
        'active_prefix': user_prefix,
        'active_prefixes': active_prefixes,
    }
    return render(request, 'apps/data/files.html', context)

@require_POST
def delete_file(request):
    """
    View for deleting files or folders.
    """
    # Initialize logger
    log = logger.get_logger()
    
    # Get the key from the request
    key = request.POST.get('key')

    try:
        if key.endswith('/'):
            delete_s3_folder(key)
            log.data.info(f"Deleted folder {key}")
            
        else:
            delete_s3_object(key)
            log.data.info(f"Deleted {key}")

    except Exception as e:
        log.data.error(f"Error deleting {key}: {e}")
    return redirect(request.META.get('HTTP_REFERER', reverse('list_files')))

@require_POST
def rename_file(request):
    """
    View for renaming files or folders.
    """
    # Initialize logger
    log = logger.get_logger()
    
    # Get the old key and new name from the request
    old_key = request.POST.get('old_key')
    new_name = request.POST.get('new_name')
    
    # Extract the directory path
    prefix = '/'.join(old_key.rstrip('/').split('/')[:-1])
    
    # Build the new key
    if prefix:
        new_key = f"{prefix}/{new_name}"
        if old_key.endswith('/'):
            new_key += '/'
    else:
        new_key = new_name
        if old_key.endswith('/'):
            new_key += '/'
    
    try:
        if old_key.endswith('/'):
            rename_s3_folder(old_key, new_key)
            log.data.info(f"Renamed folder {old_key} to {new_key}")
        else:
            rename_s3_object(old_key, new_key)
            log.data.info(f"Renamed {old_key} to {new_key}")
    except Exception as e:
        log.data.error(f"Error renaming {old_key}: {e}")

    return redirect(request.META.get('HTTP_REFERER', reverse('list_files')))

@login_required(login_url='/users/signin/')
def list_all_folders(request):
    """
    Return a flat list of all folders (recursively) in the current project's data directory.
    """
    # Get the current user's active project
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse([], safe=False)
    
    # Set the root path to be within the project's data directory
    root_path = f"{current_project_uuid}/data/"
    
    def collect_folders(prefix):
        """
        Recursively collect all folders under a prefix.
        
        Args:
            prefix (str): S3 prefix to start from
            
        Returns:
            list: All folder paths
        """
        folders, _ = list_s3_folder(prefix)
        all_folders = []
        for folder in folders:
            # Only include folders that start with the root path
            if folder.startswith(root_path):
                all_folders.append(folder)
                all_folders.extend(collect_folders(folder))
        return all_folders

    # Start collecting folders from the project's data directory
    all_folders = collect_folders(root_path)
    
    # Process the folders to make them relative to the project's data directory
    folder_list = []
    for folder in all_folders:
        # Create a display name that's relative to the data directory
        relative_path = folder[len(root_path):]
        folder_list.append({
            "label": relative_path if relative_path else "(root)",
            "value": relative_path
        })
    
    # Always include root
    if not any(item["value"] == "" for item in folder_list):
        folder_list.insert(0, {"label": "(root)", "value": ""})
    
    return JsonResponse(folder_list, safe=False)

###### New Validation Views ######
@login_required(login_url='/users/signin/')
@require_POST
def start_validation(request):
    """Start a validation run for the current project."""
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({'error': 'No project selected'}, status=400)
    
    try:
        from ..project.models import Project
        project = Project.objects.get(identifier=current_project_uuid)
        
        if not project.data_validation_script:
            return JsonResponse({'error': 'No validation script uploaded'}, status=400)
        
        # Cancel any running validation for this project
        running_validations = ValidationRun.objects.filter(
            project=project,
            status__in=['pending', 'running']
        )
        
        for validation in running_validations:
            if validation.celery_task_id:
                current_app.control.revoke(validation.celery_task_id, terminate=True)
            validation.status = 'cancelled'
            validation.completed_at = timezone.now()
            validation.save()
        
        # Create new validation run
        validation_run = ValidationRun.objects.create(
            project=project,
            user=request.user
        )
        
        # Start the task
        task = run_validation_task.delay(str(validation_run.id))
        validation_run.celery_task_id = task.id
        validation_run.save()
        
        return JsonResponse({
            'success': True,
            'validation_run_id': str(validation_run.id),
            'task_id': task.id
        })
        
    except Project.DoesNotExist:
        return JsonResponse({'error': 'Project not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@login_required(login_url='/users/signin/')
@require_POST  
def stop_validation(request):
    """Stop the currently running validation."""
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({'error': 'No project selected'}, status=400)
    
    try:
        from ..project.models import Project
        project = Project.objects.get(identifier=current_project_uuid)
        
        # Find running validation
        validation_run = ValidationRun.objects.filter(
            project=project,
            status__in=['pending', 'running']
        ).first()
        
        if not validation_run:
            return JsonResponse({'error': 'No running validation found'}, status=404)
        
        # Cancel the Celery task
        if validation_run.celery_task_id:
            current_app.control.revoke(validation_run.celery_task_id, terminate=True)
        
        # Update status
        validation_run.status = 'cancelled'
        validation_run.completed_at = timezone.now()
        validation_run.save()
        
        return JsonResponse({'success': True})
        
    except Project.DoesNotExist:
        return JsonResponse({'error': 'Project not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@login_required(login_url='/users/signin/')
def validation_status(request):
    """Get the current validation status for the project."""
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({'error': 'No project selected'}, status=400)
    
    try:
        from ..project.models import Project
        project = Project.objects.get(identifier=current_project_uuid)
        
        # Get latest validation run
        latest_validation = ValidationRun.objects.filter(project=project).first()
        
        if not latest_validation:
            return JsonResponse({
                'status': 'none',
                'checks': []
            })
        
        # Get validation checks
        checks = list(ValidationCheck.objects.filter(
            validation_run=latest_validation
        ).values('name', 'status', 'message', 'details'))
        
        return JsonResponse({
            'status': latest_validation.status,
            'success': latest_validation.success,
            'output': latest_validation.output,
            'error_message': latest_validation.error_message,
            'checks': checks,
            'started_at': latest_validation.started_at,
            'completed_at': latest_validation.completed_at
        })
        
    except Project.DoesNotExist:
        return JsonResponse({'error': 'Project not found'}, status=404)
    

@login_required(login_url='/users/signin/')
@require_POST
def start_visualization(request):
    """Start a visualization run for the current project."""
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({'error': 'No project selected'}, status=400)
    
    try:
        from ..project.models import Project
        project = Project.objects.get(identifier=current_project_uuid)
        
        if not project.data_visualization_script:
            return JsonResponse({'error': 'No visualization script uploaded'}, status=400)
        
        # Cancel any running visualization for this project
        running_visualizations = VisualizationRun.objects.filter(
            project=project,
            status__in=['pending', 'running']
        )
        
        for visualization in running_visualizations:
            if visualization.celery_task_id:
                current_app.control.revoke(visualization.celery_task_id, terminate=True)
            visualization.status = 'cancelled'
            visualization.completed_at = timezone.now()
            visualization.save()
        
        # Create new visualization run
        visualization_run = VisualizationRun.objects.create(
            project=project,
            user=request.user
        )
        
        # Start the task
        task = run_visualization_task.delay(str(visualization_run.id))
        visualization_run.celery_task_id = task.id
        visualization_run.save()
        
        return JsonResponse({
            'success': True,
            'visualization_run_id': str(visualization_run.id),
            'task_id': task.id
        })
        
    except Project.DoesNotExist:
        return JsonResponse({'error': 'Project not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@login_required(login_url='/users/signin/')
@require_POST  
def stop_visualization(request):
    """Stop the currently running visualization."""
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({'error': 'No project selected'}, status=400)
    
    try:
        from ..project.models import Project
        project = Project.objects.get(identifier=current_project_uuid)
        
        # Find running visualization
        visualization_run = VisualizationRun.objects.filter(
            project=project,
            status__in=['pending', 'running']
        ).first()
        
        if not visualization_run:
            return JsonResponse({'error': 'No running visualization found'}, status=404)
        
        # Cancel the Celery task
        if visualization_run.celery_task_id:
            current_app.control.revoke(visualization_run.celery_task_id, terminate=True)
        
        # Update status
        visualization_run.status = 'cancelled'
        visualization_run.completed_at = timezone.now()
        visualization_run.save()
        
        return JsonResponse({'success': True})
        
    except Project.DoesNotExist:
        return JsonResponse({'error': 'Project not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

@login_required(login_url='/users/signin/')
def visualization_status(request):
    """Get the current visualization status for the project."""
    current_project_uuid, is_valid = get_user_project(request)
    if not is_valid:
        return JsonResponse({'error': 'No project selected'}, status=400)
    
    try:
        from ..project.models import Project
        project = Project.objects.get(identifier=current_project_uuid)
        
        # Get latest visualization run
        latest_visualization = VisualizationRun.objects.filter(project=project).first()
        
        if not latest_visualization:
            return JsonResponse({
                'status': 'none',
                'plots': []
            })
        
        # Get visualization plots
        plots = list(VisualizationPlot.objects.filter(
            visualization_run=latest_visualization
        ).values('title', 'plot_number', 'image_data', 'svg_data'))
        
        return JsonResponse({
            'status': latest_visualization.status,
            'success': latest_visualization.success,
            'output': latest_visualization.output,
            'error_message': latest_visualization.error_message,
            'plots': plots,
            'started_at': latest_visualization.started_at,
            'completed_at': latest_visualization.completed_at
        })
        
    except Project.DoesNotExist:
        return JsonResponse({'error': 'Project not found'}, status=404)
    
