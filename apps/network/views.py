from django.shortcuts import render
from django.contrib.auth.decorators import login_required

@login_required(login_url='/users/signin/')
def network(request):

  context = {
    'segment': 'network',
  }
  return render(request, "apps/network/network.html", context)

@login_required(login_url='/users/signin/')
def new_network(request):

  context = {
    'segment': 'network',
  }
  return render(request, "apps/network/new_network.html", context)