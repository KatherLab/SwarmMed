from django.urls import path
from . import views

urlpatterns = [
    path('', views.upload_file, name="upload"),
    path('success/', views.success, name="success"),
    path('uploaded-data/', views.UploadedDataListView.as_view(), name='uploaded_data'),
]