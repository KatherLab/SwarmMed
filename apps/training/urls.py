from django.urls import path
from . import views

urlpatterns = [
    path('', views.training, name='training'),
    path('start/<uuid:network_id>/', views.start_training, name='start_training'),
]
