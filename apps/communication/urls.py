"""
URL configuration for the communication app.
Maps web addresses to the view functions that handle them.
"""

from django.urls import path

from . import views

# Set the namespace for this app's URLs
app_name = "communication"

urlpatterns = [
    # Main dashboard showing all chats and project boards
    path("", views.chat_dashboard, name="chat_dashboard"),
    # Individual chat room for a conversation with a specific user
    path("chat/<int:user_id>/", views.chat_room, name="chat_room"),
    # Project-specific discussion board
    path(
        "project/<int:project_id>/board/",
        views.project_board,
        name="project_board",
    ),
    # Compatibility redirect for '/inbox/' pointing to the chat dashboard
    path("inbox/", views.chat_dashboard, name="inbox"),
]
