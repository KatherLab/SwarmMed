import json
import os
from django.shortcuts import render, redirect
from django.http import HttpResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.urls import reverse
from ..project.models import UserCurrentProject

from .utils import (
    list_s3_folder, delete_s3_object, rename_s3_object, get_s3_download_url,
    delete_s3_folder, rename_s3_folder, get_storage_stats, format_size
)


@login_required(login_url='/users/signin/')
def data(request):
    # Get the current user's active project
    try:
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        current_project_uuid = str(user_current_project.project.identifier)
    except UserCurrentProject.DoesNotExist:
        return HttpResponse('Please select a current project first', status=400)
    
    # Set the root path to be within the project's data directory
    root_path = f"{current_project_uuid}/data/"
    
    # Get storage statistics for this project
    total_size, folder_count, file_count = get_storage_stats(root_path)
    formatted_size = format_size(total_size)
    
    context = {
        'segment': 'data',
        'folder_count': folder_count,
        'file_count': file_count,
        'space_used': formatted_size,
    }
    return render(request, "apps/data/data.html", context)

@login_required(login_url='/users/signin/')
def upload_files(request):
    # Get the current user's active project
    try:
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        current_project_uuid = str(user_current_project.project.identifier)
    except UserCurrentProject.DoesNotExist:
        return HttpResponse('Please select a current project first', status=400)
    
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
            save_path = os.path.join(full_destination, rel_path).replace('\\', '/')  # Ensure forward slashes
            default_storage.save(save_path, file)
            
        return HttpResponse('Files uploaded with folder structure preserved!')
    
    return render(request, 'apps/data/upload.html')

def get_column_prefixes(path):
    """Given a path like 'foo/bar/baz/', return ['','foo/','foo/bar/','foo/bar/baz/']"""
    if not path:
        return [""]
    parts = path.rstrip('/').split('/')
    prefixes = [""]
    for i in range(len(parts)):
        prefixes.append('/'.join(parts[:i+1]) + '/')
    return prefixes

@login_required(login_url='/users/signin/')
def list_files(request):
    # Get the current user's active project
    try:
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        current_project_uuid = str(user_current_project.project.identifier)
    except UserCurrentProject.DoesNotExist:
        return HttpResponse('Please select a current project first', status=400)
    
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
                'key': relative_folder_path
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
        'columns': columns,
        'active_prefix': user_prefix,
        'active_prefixes': active_prefixes,
    }
    return render(request, 'apps/data/files.html', context)


@require_POST
def delete_file(request):
    key = request.POST.get('key')
    try:
        if key.endswith('/'):
            delete_s3_folder(key)
            messages.success(request, f"Deleted folder {key}")
        else:
            delete_s3_object(key)
            messages.success(request, f"Deleted {key}")
    except Exception as e:
        messages.error(request, f"Error deleting {key}: {e}")
    return redirect(request.META.get('HTTP_REFERER', reverse('list_files')))


@require_POST
def rename_file(request):
    old_key = request.POST.get('old_key')
    new_name = request.POST.get('new_name')
    prefix = '/'.join(old_key.rstrip('/').split('/')[:-1])
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
            messages.success(request, f"Renamed folder {old_key} to {new_key}")
        else:
            rename_s3_object(old_key, new_key)
            messages.success(request, f"Renamed {old_key} to {new_key}")
    except Exception as e:
        messages.error(request, f"Error renaming {old_key}: {e}")
    return redirect(request.META.get('HTTP_REFERER', reverse('list_files')))

def list_all_folders(request):
    """
    Return a flat list of all folders (recursively) in the bucket.
    """
    def collect_folders(prefix=""):
        folders, _ = list_s3_folder(prefix)
        all_folders = []
        for folder in folders:
            all_folders.append(folder)
            all_folders.extend(collect_folders(folder))
        return all_folders

    all_folders = collect_folders("")
    # Remove trailing slash for display, but keep it in value
    folder_list = [{"label": f.rstrip('/'), "value": f} for f in all_folders]
    # Always include root
    folder_list.insert(0, {"label": "(root)", "value": ""})
    return JsonResponse(folder_list, safe=False)

