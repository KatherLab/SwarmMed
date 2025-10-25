from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from datetime import timedelta
from apps.users.decorators import developer_required
import os
from django.http import HttpResponse

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

@developer_required
@login_required(login_url='/users/signin/')
def download_log_category(request, category_key):
    """Download all logs for a specific category."""
    project, is_valid = get_user_project(request)
    if not is_valid:
        return HttpResponse("No project selected.", status=404)

    from .models import LogEntry, LogCategory

    # Check if the category_key is valid
    if category_key not in [choice[0] for choice in LogCategory.choices]:
        return HttpResponse("Invalid category.", status=404)

    # Get all logs for this category
    log_entries = LogEntry.objects.filter(
        project=project,
        category=category_key
    ).select_related('user').order_by('timestamp')

    # Format the logs into a string
    log_content = ""
    for entry in log_entries:
        log_content += f"[{entry.timestamp.strftime('%Y-%m-%d %H:%M:%S')}][{entry.user.email}] {entry.level} - [{entry.source}] {entry.message}\n"

    # Create the HttpResponse with the log content
    response = HttpResponse(log_content, content_type='text/plain')
    response['Content-Disposition'] = f'attachment; filename="{project.title.replace(" ", "_")}_{category_key}_logs.txt"'

    return response

@developer_required
@login_required(login_url='/users/signin/')
def logs_dashboard(request):
    """Main logs dashboard"""
    project, is_valid = get_user_project(request)
    if not is_valid:
        return render(request, "apps/logs/no_project_selected.html", {"segment": "logs"})
    
    # Query actual log entries from database
    from .models import LogEntry, LogCategory
    
    # Create a list of categories with their data (easier for template to iterate)
    categories_list = []
    
    for category_choice in LogCategory.choices:
        category_key = category_choice[0]  # e.g., 'data'
        category_display = category_choice[1]  # e.g., 'Data'
        
        # Get recent logs for this category (last 50 entries)
        recent_logs = list(LogEntry.objects.filter(
            project=project,
            category=category_key
        ).select_related('user').order_by('-timestamp')[:50])

        if category_key == 'training':
            try:
                from apps.network.models import UserCurrentNetwork
                import yaml
                import subprocess

                current_network = UserCurrentNetwork.objects.get(user=request.user).network
                if current_network:
                    project_name = project.title.replace(' ', '_')
                    compose_path = os.path.join('workspaces', str(project.identifier), str(current_network.identifier), 'workspace', project_name, 'prod_00', 'compose.yaml')

                    if os.path.exists(compose_path):
                        with open(compose_path, 'r') as f:
                            compose_data = yaml.safe_load(f)
                        
                        if compose_data and 'services' in compose_data:
                            for service_name in compose_data['services']:
                                container_name = compose_data['services'][service_name].get('container_name', service_name)
                                try:
                                    result = subprocess.run(['docker', 'logs', container_name], capture_output=True, text=True, check=False)
                                    log_output = result.stdout or result.stderr
                                    for line in log_output.splitlines():
                                        # Create a mock log entry object
                                        log_entry = {
                                            'message': line,
                                            'source': container_name,
                                            'timestamp': timezone.now(),
                                            'level': 'INFO',
                                            'user': request.user
                                        }
                                        class LogEntryObject:
                                            def __init__(self, **kwargs):
                                                self.__dict__.update(kwargs)
                                        recent_logs.insert(0, LogEntryObject(**log_entry))
                                except Exception as e:
                                    # Handle exceptions for individual container log fetching
                                    pass
            except (UserCurrentNetwork.DoesNotExist, FileNotFoundError):
                # Handle cases where network or compose file is not found
                pass
        
        # Get stats
        total_count = LogEntry.objects.filter(
            project=project,
            category=category_key
        ).count()
        
        # Recent entries (last 24 hours)
        yesterday = timezone.now() - timedelta(days=1)
        recent_count = LogEntry.objects.filter(
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
    
    context = {
        'segment': 'logs',
        'project': project,
        'categories_list': categories_list,
    }
    return render(request, "apps/logs/logs.html", context)

# Alias the function to match your URL pattern
logs = logs_dashboard


