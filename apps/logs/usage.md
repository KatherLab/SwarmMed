# Import the simplified logger
from ..logs.utils import get_logger, DATA, NETWORK, TRAINING, RESULTS, PROJECT

# Use in any view
# In Django views (auto-detects context)
from ..logs.utils import get_logger, DATA
logger = get_logger(DATA)
logger.info("This works in views!")

# In Celery tasks (explicit context)
logger = get_logger(DATA, user=task_user, project=task_project)
logger.info("This works in Celery tasks!")

# In any other file (basic logging)
logger = get_logger()
logger.info("This works anywhere!")

# In utility functions
def some_utility_function(user, project):
    logger = get_logger(DATA, user=user, project=project)
    logger.info("Processing data...")

# Example usage in a script
logger.info("This is a test")
logger.error("An error occurred")
logger.warning("This is a warning")
logger.debug("Debugging information")
logger.critical("Critical issue")