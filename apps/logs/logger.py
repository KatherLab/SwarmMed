import logging
from typing import Optional, Union
from django.contrib.auth.models import User
from .context import get_context, set_context
from .models import LogCategory

# Single logger instance - simple and efficient
_logger = logging.getLogger('app')
_setup_done = False

def _setup_logger():
    """Setup logger once"""
    global _setup_done
    if _setup_done:
        return
    
    _logger.setLevel(logging.DEBUG)
    _logger.handlers.clear()
    
    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    ))
    _logger.addHandler(console)
    
    # Database handler
    try:
        from .handlers import DatabaseLogHandler
        db_handler = DatabaseLogHandler()
        db_handler.setLevel(logging.INFO)
        _logger.addHandler(db_handler)
    except:
        pass
    
    _logger.propagate = False
    _setup_done = True

def log(level, message, category='project', user=None, project=None, **extra):
    """Universal logging function"""
    _setup_logger()
    
    # Get context
    ctx_user, ctx_project = get_context()
    final_user = user or ctx_user
    final_project = project or ctx_project
    
    # Prepare extra data
    log_extra = extra.copy()
    exc_info = log_extra.pop('exc_info', None)

    if final_user:
        log_extra['user_id'] = final_user.id
    if final_project:
        log_extra['project_id'] = final_project.identifier if hasattr(final_project, 'identifier') else str(final_project)
    
    log_extra['category'] = category
    log_extra['context_data'] = extra
    
    # Log the message
    getattr(_logger, level.lower())(message, exc_info=exc_info, extra=log_extra)

class CategoryLogger:
    """Logger for a specific category"""
    
    def __init__(self, category, user=None, project=None):
        self.category = category
        self.user = user
        self.project_obj = project  # Use different name to avoid conflict
    
    def info(self, message, **kwargs):
        log('INFO', message, category=self.category, user=self.user, project=self.project_obj, **kwargs)
    
    def error(self, message, **kwargs):
        log('ERROR', message, category=self.category, user=self.user, project=self.project_obj, **kwargs)
    
    def warning(self, message, **kwargs):
        log('WARNING', message, category=self.category, user=self.user, project=self.project_obj, **kwargs)
    
    def debug(self, message, **kwargs):
        log('DEBUG', message, category=self.category, user=self.user, project=self.project_obj, **kwargs)
    
    def critical(self, message, **kwargs):
        log('CRITICAL', message, category=self.category, user=self.user, project=self.project_obj, **kwargs)

class Logger:
    """Main logger with category properties"""
    
    def __init__(self, user=None, project=None):
        self.user_obj = user      # Use different names to avoid conflicts
        self.project_obj = project # with property names
    
    @property
    def project(self):
        return CategoryLogger(LogCategory.PROJECT, self.user_obj, self.project_obj)
    
    @property
    def data(self):
        return CategoryLogger(LogCategory.DATA, self.user_obj, self.project_obj)
    
    @property
    def network(self):
        return CategoryLogger(LogCategory.NETWORK, self.user_obj, self.project_obj)
    
    @property
    def training(self):
        return CategoryLogger(LogCategory.TRAINING, self.user_obj, self.project_obj)
    
    @property
    def results(self):
        return CategoryLogger(LogCategory.RESULTS, self.user_obj, self.project_obj)

def get_logger(user=None, project=None) -> Logger:
    """
    Get a logger with category properties
    
    Args:
        user: Optional User object (auto-detected if None)
        project: Optional Project object (auto-detected if None)
    
    Returns:
        Logger instance with category properties
    """
    # Get context
    ctx_user, ctx_project = get_context()
    final_user = user or ctx_user
    final_project = project or ctx_project
    
    return Logger(user=final_user, project=final_project)
