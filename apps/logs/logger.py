"""
Unified Logger Interface.
Provides a simple, object-oriented way to log messages into different categories
while automatically handling user/project context and database persistence.
"""

import logging

from .context import get_context
from .models import LogCategory

# Global internal logger instance
_logger = logging.getLogger('app')
_setup_done = False


def _setup_logger():
    """
    Configures the internal Python logger with appropriate handlers.
    Ensures that console and database handlers are only registered once.
    """
    global _setup_done
    if _setup_done:
        return

    _logger.setLevel(logging.DEBUG)
    _logger.handlers.clear()

    # 1. Console Handler: Prints logs to the terminal/standard output
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    ))
    _logger.addHandler(console)

    # 2. Database Handler: Saves logs to the Django database
    try:
        from .handlers import DatabaseLogHandler
        db_handler = DatabaseLogHandler()
        # Only save INFO and above to the database to avoid bloating it with
        # DEBUG logs
        db_handler.setLevel(logging.INFO)
        _logger.addHandler(db_handler)
    except Exception as e:
        # Fallback if database logging is not available (e.g., during
        # migration)
        _logger.debug(f"Database logging handler could not be initialized: {e}")

    # Disable propagation to the root logger to avoid duplicate entries in
    # some setups
    _logger.propagate = False
    _setup_done = True


def log(level, message, category='project', user=None, project=None, **extra):
    """
    Core logging function that prepares metadata and triggers the Python logger.

    Args:
        level (str): Log level (INFO, ERROR, etc.)
        message (str): The content of the log
        category (str): The logical group for this log
        user: The user object to associate with this log
        project: The project object to associate with this log
        **extra: Additional structured data to store in context_data
    """
    _setup_logger()

    # Get the current context (user/project) if not explicitly provided
    ctx_user, ctx_project = get_context()
    final_user = user or ctx_user
    final_project = project or ctx_project

    # Prepare the 'extra' dictionary for the internal Python logger
    log_extra = extra.copy()

    # Pop special keys that should not go into context_data
    exc_info = log_extra.pop('exc_info', None)
    object_id = log_extra.pop('object_id', None)

    # Inject IDs into the record so the DatabaseLogHandler can find them
    if final_user:
        log_extra['user_id'] = final_user.id

    if final_project:
        # Check if project is an object or a string identifier
        if hasattr(final_project, 'identifier'):
            log_extra['project_id'] = str(final_project.identifier)
        else:
            log_extra['project_id'] = str(final_project)

    if object_id:
        log_extra['object_id'] = str(object_id)

    log_extra['category'] = category
    log_extra['context_data'] = log_extra.copy()

    # Trigger the underlying logging call
    log_func = getattr(_logger, level.lower())
    log_func(message, exc_info=exc_info, extra=log_extra)


class CategoryLogger:
    """
    Helper class that provides standard logging methods for a specific category.
    Example usage: logger.data.info("Data processed")
    """

    def __init__(self, category, user=None, project=None):
        self.category = category
        self.user = user
        self.project_obj = project

    def info(self, message, user=None, project=None, **kwargs):
        log('INFO', message, category=self.category,
            user=user or self.user, project=project or self.project_obj, **kwargs)

    def error(self, message, user=None, project=None, **kwargs):
        log('ERROR', message, category=self.category,
            user=user or self.user, project=project or self.project_obj, **kwargs)

    def warning(self, message, user=None, project=None, **kwargs):
        log('WARNING', message, category=self.category,
            user=user or self.user, project=project or self.project_obj, **kwargs)

    def debug(self, message, user=None, project=None, **kwargs):
        log('DEBUG', message, category=self.category,
            user=user or self.user, project=project or self.project_obj, **kwargs)

    def critical(self, message, user=None, project=None, **kwargs):
        log('CRITICAL', message, category=self.category,
            user=user or self.user, project=project or self.project_obj, **kwargs)


class Logger:
    """
    Main Logger object that provides categorized access to the logging system.
    Exposes properties for each defined LogCategory.
    """

    def __init__(self, user=None, project=None):
        self.user_obj = user
        self.project_obj = project

    def log(self, level, message, category='project', user=None, project=None, **kwargs):
        """
        Generic log method that allows specifying a category string.
        """
        log(level, message, category=category,
            user=user or self.user_obj,
            project=project or self.project_obj,
            **kwargs)

    @property
    def project(self):
        """Logs related to general project actions."""
        return CategoryLogger(
            LogCategory.PROJECT,
            self.user_obj,
            self.project_obj)

    @property
    def data(self):
        """Logs related to data management and validation."""
        return CategoryLogger(
            LogCategory.DATA,
            self.user_obj,
            self.project_obj)

    @property
    def network(self):
        """Logs related to swarm network provisioning and status."""
        return CategoryLogger(
            LogCategory.NETWORK,
            self.user_obj,
            self.project_obj)

    @property
    def training(self):
        """Logs related to FL training execution and container logs."""
        return CategoryLogger(
            LogCategory.TRAINING,
            self.user_obj,
            self.project_obj)

    @property
    def results(self):
        """Logs related to result generation and analysis."""
        return CategoryLogger(
            LogCategory.RESULTS,
            self.user_obj,
            self.project_obj)

    @property
    def auth(self):
        """Logs related to authentication (login/logout)."""
        return CategoryLogger(
            LogCategory.AUTH,
            self.user_obj,
            self.project_obj)

    @property
    def access(self):
        """Logs related to access control and permission checks."""
        return CategoryLogger(
            LogCategory.ACCESS,
            self.user_obj,
            self.project_obj)


def get_logger(user=None, project=None) -> Logger:
    """
    The primary entry point to get a logger instance.

    Args:
        user: Optional User object (auto-detected from context if None)
        project: Optional Project object (auto-detected from context if None)

    Returns:
        Logger: A configured logger instance.
    """
    # Detect context if not provided
    ctx_user, ctx_project = get_context()
    final_user = user or ctx_user
    final_project = project or ctx_project

    return Logger(user=final_user, project=final_project)
