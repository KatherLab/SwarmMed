import logging

class DatabaseLogHandler(logging.Handler):
    """Custom handler that writes logs to the database"""
    
    def emit(self, record):
        try:
            # Import models inside the method to avoid circular imports
            from django.contrib.auth.models import User
            from django.apps import apps
            
            # Get models using apps.get_model to ensure apps are ready
            try:
                LogEntry = apps.get_model('logs', 'LogEntry')
                Project = apps.get_model('project', 'Project')
            except LookupError:
                # Models not ready yet, skip logging
                return
            
            user_id = getattr(record, 'user_id', None)
            project_id = getattr(record, 'project_id', None)
            category = getattr(record, 'category', 'project')
            session_id = getattr(record, 'session_id', None)
            
            if user_id and project_id:
                try:
                    user = User.objects.get(id=user_id)
                    project = Project.objects.get(identifier=project_id)
                    
                    LogEntry.objects.create(
                        user=user,
                        project=project,
                        category=category,
                        session_id=session_id,
                        level=record.levelname,
                        message=record.getMessage(),
                        context_data=getattr(record, 'context_data', {})
                    )
                except (User.DoesNotExist, Project.DoesNotExist):
                    pass
                    
        except Exception as e:
            # Don't let logging errors break the application
            self.handleError(record)


