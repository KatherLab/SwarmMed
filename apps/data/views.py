from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.contrib.auth.decorators import login_required

from .models import upload_file

@login_required(login_url='/users/signin/')
def data(request):

  context = {
    'segment': 'data',
  }
  return render(request, "apps/data/data.html", context)# Create your views here.

@login_required(login_url='/users/signin/')
def upload(request):
    if request.method == "POST":
        files = request.FILES.getlist("files")
        errors = []

        for file_obj in files:
            success, result = upload_file(file_obj)
            if not success:
                errors.append(f"Error uploading {file_obj.name}: {result}")

        if errors:
            return HttpResponse("Errors occurred: " + "; ".join(errors))
        return redirect("upload")

    return render(request, "apps/data/upload.html")