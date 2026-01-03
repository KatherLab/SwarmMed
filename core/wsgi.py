"""
WSGI configuration for the SwarmCloud project.
It exposes the WSGI callable as a module-level variable named ``application``.
This is used by standard web servers like Gunicorn or uWSGI to serve the app.
"""

import os
import sys
from pathlib import Path

from django.core.wsgi import get_wsgi_application

# Set the default settings module for the WSGI application.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

# Add the 'apps' directory to the Python path.
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR / "apps"))

# Initialize the WSGI application.
application = get_wsgi_application()
