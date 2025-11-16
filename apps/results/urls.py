from django.urls import path

from . import views

urlpatterns = [
    path("", views.results, name="results"),
    path('download/result/<int:result_id>/', views.download_result, name='download_result'),
    path('download/all/<str:project_id>/', views.download_all_results, name='download_all_results'),
    path('download/by-key/', views.download_result_by_key, name='download_result_by_key'),
]
