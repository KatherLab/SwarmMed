import json
import os
from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.urls import reverse

from .utils import list_s3_folder, delete_s3_object, rename_s3_object


@login_required(login_url='/users/signin/')
def data(request):

  context = {
    'segment': 'data',
  }
  return render(request, "apps/data/data.html", context)# Create your views here.

@login_required(login_url='/users/signin/')
def upload_files(request):
    if request.method == 'POST':
        files = request.FILES.getlist('file_field')
        directories_json = request.POST.get('directories', '{}')
        directories = json.loads(directories_json)
        for idx, file in enumerate(files):
            # Match the key used in JS
            key = file.name + '_' + str(idx)
            rel_path = directories.get(key, file.name)
            # Save to MinIO (or default storage) preserving the folder structure
            save_path = os.path.join('uploads', rel_path)
            default_storage.save(save_path, file)
        return HttpResponse('Files uploaded with folder structure preserved!')
    return render(request, 'apps/data/upload.html')

@login_required(login_url='/users/signin/') 
def list_files(request):
    # Get the current folder from query params, default to root
    prefix = request.GET.get('prefix', '')
    if prefix and not prefix.endswith('/'):
        prefix += '/'
    folders, files = list_s3_folder(prefix)
    # For navigation: build breadcrumbs
    breadcrumbs = []
    if prefix:
        parts = prefix.rstrip('/').split('/')
        for i in range(len(parts)):
            crumb_prefix = '/'.join(parts[:i+1]) + '/'
            breadcrumbs.append((parts[i], crumb_prefix))
    context = {
        'prefix': prefix,
        'folders': folders,
        'files': files,
        'breadcrumbs': breadcrumbs,
    }
    return render(request, 'apps/data/files.html', context)

@require_POST
def delete_file(request):
    key = request.POST.get('key')
    try:
        delete_s3_object(key)
        messages.success(request, f"Deleted {key}")
    except Exception as e:
        messages.error(request, f"Error deleting {key}: {e}")
    return redirect(request.META.get('HTTP_REFERER', reverse('list_files')))

@require_POST
def rename_file(request):
    old_key = request.POST.get('old_key')
    new_name = request.POST.get('new_name')
    # Compute new key in the same folder
    prefix = '/'.join(old_key.split('/')[:-1])
    new_key = f"{prefix}/{new_name}" if prefix else new_name
    try:
        rename_s3_object(old_key, new_key)
        messages.success(request, f"Renamed {old_key} to {new_key}")
    except Exception as e:
        messages.error(request, f"Error renaming {old_key}: {e}")
    return redirect(request.META.get('HTTP_REFERER', reverse('list_files')))