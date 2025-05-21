from django.urls import path

from . import views

urlpatterns = [
    path("", views.data, name="data"),
    path('upload/', views.upload_files, name='upload_files'),
    path('files/', views.list_files, name='list_files'),
    path('files/delete/', views.delete_file, name='delete_file'),
    path('files/rename/', views.rename_file, name='rename_file'),
    path('folders/', views.list_all_folders, name='list_all_folders'),

]