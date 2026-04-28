"""Celery configuration for the SwarmMedHub project.
This module initializes the Celery application and configures it to use
the Django settings. It also enables automatic discovery of tasks in
all registered Django apps.
"""

import os
import sys
from pathlib import Path

from celery import Celery

# Set the default Django settings module for the 'celery' program.
# This ensures Celery can access Django's database and settings.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

# Add the 'apps' directory to the Python path.
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR / "apps"))

# Initialize the Celery app with the project name.
app = Celery("core")

# Load the Celery configuration from the Django settings file.
# The 'namespace' argument means all Celery-related settings must have
# the 'CELERY_' prefix (e.g., CELERY_BROKER_URL).
app.config_from_object("django.conf:settings", namespace="CELERY")

# Automatically search for 'tasks.py' files in each installed Django app.
# This allows tasks to be defined locally within each application.
app.autodiscover_tasks()
