# Import the simplified logger
from ..logs.utils import get_user_project_logger, DATA, NETWORK, TRAINING, RESULTS, PROJECT

# Use in any view
logger = get_user_project_logger(DATA)  # or NETWORK, TRAINING, RESULTS, PROJECT
logger.info("This is a test")
logger.error("An error occurred")
logger.warning("This is a warning")
logger.debug("Debugging information")
logger.critical("Critical issue")
logger.end_session('completed')