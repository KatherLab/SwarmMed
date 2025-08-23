from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from .utils import get_tailscale_ip, is_tailscale_connected

@login_required(login_url='/users/signin/')
def network(request):

  context = {
    'segment': 'network',
    'tailscale_status': is_tailscale_connected(),
    'tailscale_ip': get_tailscale_ip(),
  }
  return render(request, "apps/network/network.html", context)

@login_required(login_url='/users/signin/')
def new_network(request):

  context = {
    'segment': 'network',
  }
  return render(request, "apps/network/new_network.html", context)