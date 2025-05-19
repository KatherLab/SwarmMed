from django.shortcuts import render
from django.contrib.auth.decorators import login_required

@login_required(login_url='/users/signin/')
def training(request):

  context = {
    'segment': 'training',
  }
  return render(request, "pages/training.html", context)
