import logging

class DatabaseLogHandler(logging.Handler):
    """Simple database handler with graceful fallbacks"""
    
    def emit(self, record):
        try:
            # Only try database logging if we have context
            user_id = getattr(record, 'user_id', None)
            project_id = getattr(record, 'project_id', None)
            
            if not (user_id and project_id):
                return  # Skip database logging without context
            
            from django.contrib.auth.models import User
            from django.apps import apps
            
            # Check if models are ready
            try:
                LogEntry = apps.get_model('logs', 'LogEntry')
                Project = apps.get_model('project', 'Project')
            except LookupError:
                return  # Models not ready, skip
            
            user = User.objects.get(id=user_id)
            project = Project.objects.get(identifier=project_id)
            
            LogEntry.objects.create(
                user=user,
                project=project,
                category=getattr(record, 'category', 'project'),
                level=record.levelname,
                message=record.getMessage(),
                context_data=getattr(record, 'context_data', {})
            )
            
        except Exception:
            # Silently fail - don't break the application
            pass





