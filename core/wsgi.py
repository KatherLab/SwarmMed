"""
WSGI configuration for the SwarmCloud project.
It exposes the WSGI callable as a module-level variable named ``application``.
This is used by standard web servers like Gunicorn or uWSGI to serve the app.
"""

import os

from django.core.wsgi import get_wsgi_application

# Set the default settings module for the WSGI application.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

# Initialize the WSGI application.
application = get_wsgi_application()
