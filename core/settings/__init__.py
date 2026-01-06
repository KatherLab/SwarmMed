import os

from dotenv import load_dotenv
from str2bool import str2bool

# Load environment variables from a .env file into os.environ.
load_dotenv()

DEBUG = str2bool(os.environ.get("DEBUG", "False"))

if DEBUG:
    from .development import *  # noqa: F403
else:
    from .production import *  # noqa: F403

# Explicitly export all locals to the module level
# This ensures that all settings imported from dev/prod are available
# to Django's settings loader.
globals().update(locals())
