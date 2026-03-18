"""Unified Logger Interface.
Provides a simple, object-oriented way to log messages into different categories
while automatically handling user/project context and database persistence.
"""

import logging

from .context import get_context
from .models import LogCategory

# Global internal logger instance
_logger = logging.getLogger("app")
_setup_done = False


def _setup_logger():
    """Configures the internal Python logger with appropriate handlers.
    Ensures that console and database handlers are only registered once.
    """
    global _setup_done
    if _setup_done:
        return

    _logger.setLevel(logging.DEBUG)
    _logger.handlers.clear()

    # 1. Console Handler: Prints logs to the terminal/standard output
    console = logging.StreamHandler()
    console.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
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
        _logger.debug(
            f"Database logging handler could not be initialized: {e}"
        )

    # Disable propagation to the root logger to avoid duplicate entries in
    # some setups
    _logger.propagate = False
    _setup_done = True


def log(level, message, category="project", user=None, project=None, network=None, **extra):
    """Core logging function that prepares metadata and triggers the Python logger.

    Args:
        level (str): Log level (INFO, ERROR, etc.)
        message (str): The content of the log
        category (str): The logical group for this log
        user: The user object to associate with this log
        project: The project object to associate with this log
        network: The swarm network object to associate with this log
        **extra: Additional structured data to store in context_data
    """
    _setup_logger()

    # Get the current context (user/project/network) if not explicitly provided
    ctx_user, ctx_project, ctx_network = get_context()
    
    final_user = user or ctx_user
    final_project = project or ctx_project
    final_network = network or ctx_network

    # Prepare the 'extra' dictionary for the internal Python logger
    log_extra = extra.copy()

    # HIPAA: Detect emergency access and flag the log entry
    is_emergency = False
    if final_user and hasattr(final_user, "profile"):
        is_emergency = final_user.profile.is_emergency_access

    if is_emergency:
        log_extra["is_emergency"] = True
        message = f"[EMERGENCY] {message}"
        # Force minimum level INFO for emergency logs to ensure database persistence
        if level == "DEBUG":
            level = "INFO"

    # Pop special keys that should not go into context_data
    exc_info = log_extra.pop("exc_info", None)
    object_id = log_extra.pop("object_id", None)

    # Inject IDs into the record so the DatabaseLogHandler can find them
    if final_user:
        log_extra["user_id"] = final_user.id

    if final_project:
        # We store the primary key (integer ID) in the log_extra record
        # so it can be correctly linked to the Project model via ForeignKey.
        if hasattr(final_project, "id"):
            log_extra["project_id"] = final_project.id
        else:
            log_extra["project_id"] = final_project

    if final_network:
        if hasattr(final_network, "id"):
            log_extra["swarm_network_id"] = final_network.id
        else:
            log_extra["swarm_network_id"] = final_network

    if object_id:
        log_extra["object_id"] = str(object_id)

    log_extra["category"] = category
    log_extra["context_data"] = log_extra.copy()

    # Trigger the underlying logging call
    log_func = getattr(_logger, level.lower())
    log_func(message, exc_info=exc_info, extra=log_extra)


class CategoryLogger:
    """Helper class that provides standard logging methods for a specific category.
    Example usage: logger.data.info("Data processed").
    """

    def __init__(self, category, user=None, project=None, network=None):
        self.category = category
        self.user = user
        self.project_obj = project
        self.network_obj = network

    def info(self, message, user=None, project=None, network=None, **kwargs):
        log(
            "INFO",
            message,
            category=self.category,
            user=user or self.user,
            project=project or self.project_obj,
            network=network or self.network_obj,
            **kwargs,
        )

    def error(self, message, user=None, project=None, network=None, **kwargs):
        log(
            "ERROR",
            message,
            category=self.category,
            user=user or self.user,
            project=project or self.project_obj,
            network=network or self.network_obj,
            **kwargs,
        )

    def warning(self, message, user=None, project=None, network=None, **kwargs):
        log(
            "WARNING",
            message,
            category=self.category,
            user=user or self.user,
            project=project or self.project_obj,
            network=network or self.network_obj,
            **kwargs,
        )

    def debug(self, message, user=None, project=None, network=None, **kwargs):
        log(
            "DEBUG",
            message,
            category=self.category,
            user=user or self.user,
            project=project or self.project_obj,
            network=network or self.network_obj,
            **kwargs,
        )

    def critical(self, message, user=None, project=None, network=None, **kwargs):
        log(
            "CRITICAL",
            message,
            category=self.category,
            user=user or self.user,
            project=project or self.project_obj,
            network=network or self.network_obj,
            **kwargs,
        )


class Logger:
    """Main Logger object that provides categorized access to the logging system.
    Exposes properties for each defined LogCategory.
    """

    def __init__(self, user=None, project=None, network=None):
        self.user_obj = user
        self.project_obj = project
        self.network_obj = network

    def log(
        self,
        level,
        message,
        category="project",
        user=None,
        project=None,
        network=None,
        **kwargs,
    ):
        """Generic log method that allows specifying a category string."""
        log(
            level,
            message,
            category=category,
            user=user or self.user_obj,
            project=project or self.project_obj,
            network=network or self.network_obj,
            **kwargs,
        )

    @property
    def project(self):
        """Logs related to general project actions."""
        return CategoryLogger(
            LogCategory.PROJECT, self.user_obj, self.project_obj, self.network_obj
        )

    @property
    def data(self):
        """Logs related to data management and validation."""
        return CategoryLogger(
            LogCategory.DATA, self.user_obj, self.project_obj, self.network_obj
        )

    @property
    def network(self):
        """Logs related to swarm network provisioning and status."""
        return CategoryLogger(
            LogCategory.NETWORK, self.user_obj, self.project_obj, self.network_obj
        )

    @property
    def training(self):
        """Logs related to FL training execution and container logs."""
        return CategoryLogger(
            LogCategory.TRAINING, self.user_obj, self.project_obj, self.network_obj
        )

    @property
    def results(self):
        """Logs related to result generation and analysis."""
        return CategoryLogger(
            LogCategory.RESULTS, self.user_obj, self.project_obj, self.network_obj
        )

    @property
    def auth(self):
        """Logs related to authentication (login/logout). Re-mapped to project."""
        return CategoryLogger(
            LogCategory.PROJECT, self.user_obj, self.project_obj, self.network_obj
        )

    @property
    def access(self):
        """Logs related to access control and permission checks. Re-mapped to project."""
        return CategoryLogger(
            LogCategory.PROJECT, self.user_obj, self.project_obj, self.network_obj
        )


def get_logger(user=None, project=None, network=None) -> Logger:
    """The primary entry point to get a logger instance.

    Args:
        user: Optional User object (auto-detected from context if None)
        project: Optional Project object (auto-detected from context if None)
        network: Optional SwarmNetwork object (auto-detected from context if None)

    Returns:
        Logger: A configured logger instance.
    """
    # Detect context if not provided
    ctx_user, ctx_project, ctx_network = get_context()
    
    final_user = user or ctx_user
    final_project = project or ctx_project
    final_network = network or ctx_network

    return Logger(user=final_user, project=final_project, network=final_network)
