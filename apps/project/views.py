from django.shortcuts import render
from django.contrib.auth.decorators import login_required

@login_required(login_url='/users/signin/')
def project(request):

  context = {
    'segment': 'project',
  }
  return render(request, "apps/project/project.html", context)

login_required(login_url='/users/signin/')
def new_project(request):

  context = {
    'segment': 'project',
  }
  return render(request, "apps/project/new_project.html", context)

# Create your views here.
