# apps/logs/views.py
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from datetime import timedelta

# Import the get_user_project function from your project app
try:
    from ..project.models import UserCurrentProject
except ImportError:
    # Fallback if the import fails
    UserCurrentProject = None

def get_user_project(request):
    """Get the current user's active project"""
    if not UserCurrentProject:
        return None, False
        
    try:
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        if not user_current_project.project:
            return None, False
        return user_current_project.project, True
    except UserCurrentProject.DoesNotExist:
        return None, False

@login_required(login_url='/users/signin/')
def logs_dashboard(request):
    """Main logs dashboard"""
    project, is_valid = get_user_project(request)
    if not is_valid:
        return render(request, "apps/logs/no_project_selected.html", {"segment": "logs"})
    
    # Query actual log entries from database
    from .models import LogEntry, LogSession, LogCategory
    
    # Create a list of categories with their data (easier for template to iterate)
    categories_list = []
    
    for category_choice in LogCategory.choices:
        category_key = category_choice[0]  # e.g., 'data'
        category_display = category_choice[1]  # e.g., 'Data'
        
        # Get recent logs for this category (last 50 entries)
        recent_logs = LogEntry.objects.filter(
            user=request.user,
            project=project,
            category=category_key
        ).order_by('-timestamp')[:50]
        
        # Get stats
        total_count = LogEntry.objects.filter(
            user=request.user,
            project=project,
            category=category_key
        ).count()
        
        # Recent entries (last 24 hours)
        yesterday = timezone.now() - timedelta(days=1)
        recent_count = LogEntry.objects.filter(
            user=request.user,
            project=project,
            category=category_key,
            timestamp__gte=yesterday
        ).count()
        
        categories_list.append({
            'key': category_key,
            'display_name': category_display,
            'entries': recent_logs,
            'total_entries': total_count,
            'recent_entries': recent_count,
        })
    
    # Get recent sessions
    recent_sessions = LogSession.objects.filter(
        user=request.user,
        project=project
    ).order_by('-started_at')[:10]
    
    context = {
        'segment': 'logs',
        'project': project,
        'categories_list': categories_list,  # Changed from categories_data
        'recent_sessions': recent_sessions,
    }
    return render(request, "apps/logs/logs.html", context)

# Alias the function to match your URL pattern
logs = logs_dashboard

