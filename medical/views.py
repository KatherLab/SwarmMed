from django.shortcuts import render, redirect
from .forms import MedicalDataForm
from django.views.generic import ListView
from .models import MedicalData

def upload_file(request):
    if request.method == 'POST':
        form = MedicalDataForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            return redirect('success')
    else:
        form = MedicalDataForm()
    return render(request, 'medical/upload.html', {'form': form})

def success(request):
    return render(request, 'medical/success.html')

class UploadedDataListView(ListView):
    model = MedicalData
    template_name = 'medical/uploaded_data.html'
    context_object_name = 'uploaded_data'