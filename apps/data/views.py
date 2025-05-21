import json
import os
from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.urls import reverse

from .utils import list_s3_folder, delete_s3_object, rename_s3_object, get_s3_download_url


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

def get_column_prefixes(path):
    """Given a path like 'foo/bar/baz/', return ['','foo/','foo/bar/','foo/bar/baz/']"""
    if not path:
        return [""]
    parts = path.rstrip('/').split('/')
    prefixes = [""]
    for i in range(len(parts)):
        prefixes.append('/'.join(parts[:i+1]) + '/')
    return prefixes

def list_files(request):
    # Get the current folder from query params, default to root
    prefix = request.GET.get('prefix', '')
    column_prefixes = get_column_prefixes(prefix)
    columns = []
    for col_prefix in column_prefixes:
        folders, files = list_s3_folder(col_prefix)
        # Only show immediate children (strip prefix)
        folders = [{'name': f[len(col_prefix):-1], 'key': f} for f in folders]
        files = [{'name': f[len(col_prefix):], 'key': f, 'download_url': get_s3_download_url(f)} for f in files]
        columns.append({'prefix': col_prefix, 'folders': folders, 'files': files})
    # Build active_prefixes for highlighting
    active_prefixes = set()
    if prefix:
        parts = prefix.rstrip('/').split('/')
        for i in range(len(parts)):
            active_prefixes.add('/'.join(parts[:i+1]) + '/')
    context = {
        'columns': columns,
        'active_prefix': prefix,
        'active_prefixes': active_prefixes,
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
        rename_s3_object(old_key, new_key)
        messages.success(request, f"Renamed {old_key} to {new_key}")
    except Exception as e:
        messages.error(request, f"Error renaming {old_key}: {e}")
    return redirect(request.META.get('HTTP_REFERER', reverse('list_files')))