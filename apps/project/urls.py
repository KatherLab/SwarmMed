from django.urls import path
from . import views

urlpatterns = [
    path('', views.project_list, name='project_list'),
    path('new/', views.project_create, name='project_create'),
    path('edit/<int:pk>/', views.project_edit, name='project_edit'),
    path('delete/<int:pk>/', views.project_delete, name='project_delete'),
    path('set-current/<int:pk>/', views.set_current_project, name='set_current_project'),
]