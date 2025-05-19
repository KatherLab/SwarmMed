from django.shortcuts import render
from django.contrib.auth.decorators import login_required

@login_required(login_url='/users/signin/')
def results(request):

  context = {
    'segment': 'results',
  }
  return render(request, "pages/results.html", context)