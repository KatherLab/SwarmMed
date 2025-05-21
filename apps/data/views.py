from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required



import json
import os
from django.conf import settings
from django.core.files.storage import default_storage
from django.shortcuts import render, redirect
from django.http import HttpResponse


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