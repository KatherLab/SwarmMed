from django.urls import path
from . import views

urlpatterns = [
    path("", views.data, name="data"),
    
    # Data file endpoints
    path('upload/', views.upload_files, name='upload_files'),
    path('files/', views.list_files, name='list_files'),
    path('files/delete/', views.delete_file, name='delete_file'),
    path('files/rename/', views.rename_file, name='rename_file'),
    path('folders/', views.list_all_folders, name='list_all_folders'),
    
    # Validation endpoints
    path('validation/start/', views.start_validation, name='start_validation'),
    path('validation/stop/', views.stop_validation, name='stop_validation'), 
    path('validation/status/', views.validation_status, name='validation_status'),
    
    # Visualization endpoints
    path('visualization/start/', views.start_visualization, name='start_visualization'),
    path('visualization/stop/', views.stop_visualization, name='stop_visualization'), 
    path('visualization/status/', views.visualization_status, name='visualization_status'),
]
