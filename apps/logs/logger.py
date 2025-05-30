import logging
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
    if final_user:
        log_extra['user_id'] = final_user.id
    if final_project:
        log_extra['project_id'] = final_project.identifier if hasattr(final_project, 'identifier') else str(final_project)
    
    log_extra['category'] = category
    log_extra['context_data'] = extra
    
    # Log the message
    getattr(_logger, level.lower())(message, extra=log_extra)

# Simple functions for each level
def info(message, **kwargs):
    log('INFO', message, **kwargs)

def error(message, **kwargs):
    log('ERROR', message, **kwargs)

def warning(message, **kwargs):
    log('WARNING', message, **kwargs)

def debug(message, **kwargs):
    log('DEBUG', message, **kwargs)

# Category shortcuts
def log_data(message, level='INFO', **kwargs):
    log(level, message, category=LogCategory.DATA, **kwargs)

def log_training(message, level='INFO', **kwargs):
    log(level, message, category=LogCategory.TRAINING, **kwargs)

def log_network(message, level='INFO', **kwargs):
    log(level, message, category=LogCategory.NETWORK, **kwargs)

def log_results(message, level='INFO', **kwargs):
    log(level, message, category=LogCategory.RESULTS, **kwargs)
