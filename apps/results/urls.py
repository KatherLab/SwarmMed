from django.urls import path

from . import views

urlpatterns = [
    path("", views.results, name="results"),
    path('download/result/<int:result_id>/', views.download_result, name='download_result'),
    path('sync/', views.sync_results, name='sync_results'),

    path('download/all/<str:project_id>/', views.download_all_results, name='download_all_results'),
]