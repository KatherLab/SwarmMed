import os
from str2bool import str2bool
from dotenv import load_dotenv

# Load environment variables from a .env file into os.environ.
load_dotenv()

DEBUG = str2bool(os.environ.get("DEBUG", "False"))

if DEBUG:
    from .development import *
else:
    from .production import *

# Explicitly export all locals to the module level
# This ensures that all settings imported from dev/prod are available
# to Django's settings loader.
globals().update(locals())