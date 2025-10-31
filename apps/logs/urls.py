from django.urls import path

from . import views

urlpatterns = [
    path("", views.logs, name="logs"),
    path('download/<str:category_key>/', views.download_log_category, name='download_log_category'),
]