import os

from str2bool import str2bool

from .base import *

DEBUG = False

# Production-specific database options
DATABASES["default"]["OPTIONS"]["sslmode"] = "verify-full"

# Production-specific storage (e.g., WhiteNoise for static files)
STORAGES["staticfiles"][
    "BACKEND"
] = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# Enable cached template loader in production for performance
TEMPLATES[0]["OPTIONS"]["loaders"] = [
    (
        "django.template.loaders.cached.Loader",
        [
            "django.template.loaders.filesystem.Loader",
            "django.template.loaders.app_directories.Loader",
        ],
    ),
]

# Security Hardening
SECURE_SSL_REDIRECT = str2bool(os.environ.get("SECURE_SSL_REDIRECT", "True"))
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = 31536000  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
