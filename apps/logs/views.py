from django.shortcuts import render
from django.contrib.auth.decorators import login_required

@login_required(login_url='/users/signin/')
def logs(request):

  context = {
    'segment': 'logs',
  }
  return render(request, "apps/logs.html", context)