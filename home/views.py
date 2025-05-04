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

  context = {}
  return render(request, "pages/data.html", context)

def network(request):

  context = {}
  return render(request, "pages/network.html", context)

def training(request):

  context = {}
  return render(request, "pages/training.html", context)

def logs(request):

  context = {}
  return render(request, "pages/logs.html", context)