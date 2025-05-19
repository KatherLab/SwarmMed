from django.shortcuts import render
from django.contrib.auth.decorators import login_required

@login_required(login_url='/users/signin/')
def data(request):

  context = {
    'segment': 'data',
  }
  return render(request, "pages/data.html", context)# Create your views here.
