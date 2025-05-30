import logging
from .middleware import get_current_request
from .models import LogCategory

class UserProjectLogger:
    """Utility class for user/project-specific logging"""
    
    def __init__(self, user, project, category='project'):
        self.user = user
        self.project = project
        self.category = category
        self.logger = logging.getLogger('user_logs')
    
    def _add_context(self, extra=None):
        """Add user/project context to log records"""
        context = {
            'user_id': self.user.id,
            'project_id': str(self.project.identifier),
            'category': self.category,
        }
        if extra:
            context.update(extra)
        return context
    
    def info(self, message, extra=None):
        self.logger.info(message, extra=self._add_context(extra))
    
    def error(self, message, extra=None):
        self.logger.error(message, extra=self._add_context(extra))
    
    def warning(self, message, extra=None):
        self.logger.warning(message, extra=self._add_context(extra))
    
    def debug(self, message, extra=None):
        self.logger.debug(message, extra=self._add_context(extra))
    
    def critical(self, message, extra=None):
        self.logger.critical(message, extra=self._add_context(extra))

def get_user_project_logger(category):
    """
    Simplified logger factory that automatically detects user and project
    
    Args:
        category: LogCategory enum value (e.g., DATA)
    
    Returns:
        UserProjectLogger instance
    
    Usage:
        logger = get_user_project_logger(DATA)
        logger.info("This is a test")
    """
    # Get current request from thread-local storage
    request = get_current_request()
    
    if not request or not request.user.is_authenticated:
        # Return a no-op logger if no request context or user
        return NoOpLogger()
    
    # Get current project
    try:
        from ..project.models import UserCurrentProject
        
        user_current_project = UserCurrentProject.objects.get(user=request.user)
        if not user_current_project.project:
            return NoOpLogger()
        
        project = user_current_project.project
        
        # Convert category to string if it's an enum
        if hasattr(category, 'value'):
            category_str = category.value
        else:
            category_str = str(category).lower()
        
        return UserProjectLogger(request.user, project, category_str)
        
    except Exception:
        # Return no-op logger if anything fails
        return NoOpLogger()

# Convenience constants for categories
DATA = LogCategory.DATA
NETWORK = LogCategory.NETWORK
TRAINING = LogCategory.TRAINING
RESULTS = LogCategory.RESULTS
PROJECT = LogCategory.PROJECT

class NoOpLogger:
    """No-operation logger for when context is not available"""
    
    def info(self, message, extra=None):
        pass
    
    def error(self, message, extra=None):
        pass
    
    def warning(self, message, extra=None):
        pass
    
    def debug(self, message, extra=None):
        pass
    
    def critical(self, message, extra=None):
        pass
