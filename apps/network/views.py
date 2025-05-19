from django.shortcuts import render
from django.contrib.auth.decorators import login_required

@login_required(login_url='/users/signin/')
def network(request):

  context = {
    'segment': 'network',
  }
  return render(request, "pages/network.html", context)

@login_required(login_url='/users/signin/')
def new_network(request):

  context = {
    'segment': 'network',
  }
  return render(request, "pages/new_network.html", context)