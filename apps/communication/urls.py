from django.urls import path
from . import views

app_name = 'communication'

urlpatterns = [
    path('', views.chat_dashboard, name='chat_dashboard'),
    path('chat/<int:user_id>/', views.chat_room, name='chat_room'),
    path('project/<int:project_id>/board/', views.project_board, name='project_board'),
    
    # Kept for compatibility if needed, but redirects are better in logic
    path('inbox/', views.chat_dashboard, name='inbox'), 
]