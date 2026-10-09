import contextlib
import os

from dotenv import load_dotenv
from str2bool import str2bool

# Load environment variables from a .env file into os.environ.
# The containers already get these variables through env_file, and .env is readable
# only by its owner, so a .env the process cannot read is skipped.
with contextlib.suppress(PermissionError):
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
