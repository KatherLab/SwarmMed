"""
ASGI configuration for the SwarmCloud project.
It exposes the ASGI callable as a module-level variable named ``application``.
This is used by asynchronous servers like Daphne or Uvicorn to serve the app.
"""

import os

from django.core.asgi import get_asgi_application

# Set the default settings module for the ASGI application.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

# Initialize the ASGI application.
application = get_asgi_application()
