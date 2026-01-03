from .base import *

DEBUG = True

if "debug_toolbar" not in INSTALLED_APPS:
    INSTALLED_APPS.append("debug_toolbar")

if "debug_toolbar.middleware.DebugToolbarMiddleware" not in MIDDLEWARE:
    index = MIDDLEWARE.index("django.middleware.common.CommonMiddleware")
    MIDDLEWARE.insert(index + 1, "debug_toolbar.middleware.DebugToolbarMiddleware")

# Development-specific database options
DATABASES["default"]["OPTIONS"]["sslmode"] = "require"

# Standard template loaders for development
TEMPLATES[0]["OPTIONS"]["loaders"] = [
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
]