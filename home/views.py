from django.shortcuts import render
from django.contrib.auth.decorators import login_required

from .models import *

def starter(request):

  context = {}
  return render(request, "pages/starter.html", context)

@login_required(login_url='/users/signin/')

def index(request):

  context = {
    'segment': 'dashboard',
  }
  return render(request, "dashboard/index.html", context)

def data(request):

  context = {
    'segment': 'data',
  }
  return render(request, "pages/data.html", context)

def network(request):

  context = {
    'segment': 'network',
  }
  return render(request, "pages/network.html", context)

def training(request):

  context = {
    'segment': 'training',
  }
  return render(request, "pages/training.html", context)

def logs(request):

  context = {
    'segment': 'logs',
  }
  return render(request, "pages/logs.html", context)

#### new code ####
from .forms import DocumentForm

def upload_document(request):
    uploaded = False  # Flag to indicate if the file was successfully uploaded
    if request.method == 'POST':
        form = DocumentForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()  # Automatically uploads the file to MinIO via the storage backend
            uploaded = True
            # Reinitialize a new form instance so the form is blank after upload
            form = DocumentForm()
    else:
        form = DocumentForm()
    return render(request, 'pages/upload.html', {'form': form, 'uploaded': uploaded})