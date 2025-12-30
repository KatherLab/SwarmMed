from django.db.models import Q
from .models import Message, ProjectPost, ProjectBoardAccess
from apps.project.models import Project

def unread_messages(request):
    if not request.user.is_authenticated:
        return {'unread_message_count': 0}

    user = request.user
    
    # 1. Direct Messages
    dm_count = Message.objects.filter(recipient=user, is_read=False).count()
    
    # 2. Project Board Posts
    # Get projects where user is author or member
    user_projects = Project.objects.filter(Q(author=user) | Q(members=user)).distinct()
    
    board_unread_count = 0
    for project in user_projects:
        try:
            access_log = ProjectBoardAccess.objects.get(user=user, project=project)
            last_accessed = access_log.last_accessed
        except ProjectBoardAccess.DoesNotExist:
            # If never accessed, assume all posts are unread? 
            # Or maybe none if we want to be less annoying initially. 
            # Let's say we count all posts since project creation if never accessed, 
            # but that might be too many. Let's assume a default "start time" or just 0 if never clicked.
            # A better UX: if never accessed, show badge if there are ANY posts.
            last_accessed = None
        
        if last_accessed:
            count = ProjectPost.objects.filter(project=project, timestamp__gt=last_accessed).count()
        else:
            # If never accessed, count all posts
            count = ProjectPost.objects.filter(project=project).count()
            
        board_unread_count += count

    total_count = dm_count + board_unread_count
    
    return {'unread_message_count': total_count}