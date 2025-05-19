from django.shortcuts import render
from django.contrib.auth.decorators import login_required

@login_required(login_url='/users/signin/')
def data(request):

  context = {
    'segment': 'data',
  }
  return render(request, "pages/data.html", context)# Create your views here.

from .forms import DocumentForm

@login_required(login_url='/users/signin/')
def upload(request):
    uploaded = False  # Flag to indicate if the file was successfully uploaded
    if request.method == 'POST':
        form = DocumentForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()  # Automatically uploads the file to MinIO via the storage backend
            uploaded = True
            # Reinitialize a new form instance so the form is blank after upload
            form = DocumentForm()
    else:
        form = DocumentForm()
    return render(request, 'pages/upload.html', {'form': form, 'uploaded': uploaded})