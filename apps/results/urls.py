from django.urls import path

from . import views

urlpatterns = [
    path("", views.results, name="results"),
    path('download/result/<int:result_id>/', views.download_result, name='download_result'),
    path('download/all/<str:project_id>/', views.download_all_results, name='download_all_results'),
    path('download/by-key/', views.download_result_by_key, name='download_result_by_key'),

    # Results Visualization URLs
    path('visualize/start/<str:job_id>/', views.start_results_visualization, name='start_results_visualization'),
    path('visualize/stop/', views.stop_results_visualization, name='stop_results_visualization'),
    path('visualize/status/<str:job_id>/', views.results_visualization_status, name='results_visualization_status'),
]
