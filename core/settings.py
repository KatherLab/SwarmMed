"""
Django settings for the SwarmCloud project.
This file contains the configuration for the entire web application, including
database connections, security keys, installed apps, and middleware.
"""

import os
import random
import string
from pathlib import Path

from django.contrib import messages
from dotenv import load_dotenv
from str2bool import str2bool

# Load environment variables from a .env file into os.environ.
# This is used for sensitive information like passwords and API keys.
load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
# BASE_DIR points to the root directory of the project.
BASE_DIR = Path(__file__).resolve().parent.parent

# --- Security Settings ---

# The SECRET_KEY is used for cryptographic signing.
# We try to get it from the environment; if not found, we generate a
# random one.
SECRET_KEY = os.environ.get(
    'SECRET_KEY',
    ''.join(random.choices(string.ascii_letters + string.digits, k=50))
)

# DEBUG mode should be True for development and False for production.
DEBUG = str2bool(os.environ.get('DEBUG', 'False'))

# ALLOWED_HOSTS defines which domain names can access this server.
ALLOWED_HOSTS = ['*']

# CSRF_TRUSTED_ORIGINS is required for cross-site request forgery protection
# when running on specific domains or ports.
CSRF_TRUSTED_ORIGINS = [
    'http://localhost:8000',
    'http://localhost:5085',
    'http://127.0.0.1:8000',
    'http://127.0.0.1:5085',
    'https://rocket-django.onrender.com',
    'http://192.168.33.105:8000'
]

# IPs allowed to see the Django Debug Toolbar.
INTERNAL_IPS = ["127.0.0.1"]

# --- Application Definition ---

# List of Django apps enabled for this project.
INSTALLED_APPS = [
    # Core Django apps.
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Custom project-specific apps.
    "home",
    "apps.users",
    "apps.project",
    "apps.data",
    "apps.network",
    "apps.training",
    "apps.results",
    "apps.logs",
    "apps.communication",

    # Third-party extensions.
    "debug_toolbar",
    'storages',
]

# Middleware components process requests and responses globally.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # For static file serving.
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "debug_toolbar.middleware.DebugToolbarMiddleware",
    "apps.logs.context.RequestContextMiddleware",  # Custom context logging.
]

# The main URL configuration for the project.
ROOT_URLCONF = "core.urls"

# Location of HTML template files.
UI_TEMPLATES = os.path.join(BASE_DIR, 'templates')

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [UI_TEMPLATES],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Custom context processor for unread message counts.
                "apps.communication.context_processors.unread_messages",
            ],
        },
    },
]

# Path to the WSGI entry point for production servers.
WSGI_APPLICATION = "core.wsgi.application"

# --- Database Configuration ---

# Uses environment variables to securely connect to the database.
DATABASES = {
    'default': {
        'ENGINE': os.getenv('DB_ENGINE'),
        'NAME': os.getenv('DB_NAME'),
        'USER': os.getenv('DB_USER'),
        'PASSWORD': os.getenv('DB_PASS'),
        'HOST': os.getenv('DB_HOST'),
        'PORT': os.getenv('DB_PORT'),
    },
}

# --- Password Validation ---

# Standard Django rules to ensure users choose strong passwords.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Internationalization ---

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static and Media Files ---

# Static files (CSS, JavaScript, Images) for the UI.
STATIC_URL = "static/"
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = (os.path.join(BASE_DIR, 'static'),)

# Media files (uploaded by users, such as CSV datasets).
MEDIA_URL = 'media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Default primary key field type for models.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Authentication and Email ---

LOGIN_REDIRECT_URL = '/'

# Email configuration for password resets and notifications.
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST')
EMAIL_PORT = os.environ.get('EMAIL_PORT')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS')
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD')

# SESSION_COOKIE_HTTPONLY is False to allow some frontend integrations.
SESSION_COOKIE_HTTPONLY = False

# Mapping Django message levels to Tailwind CSS classes.
MESSAGE_TAGS = {
    messages.INFO: (
        'text-blue-800 border border-blue-300 bg-blue-50 '
        'dark:text-blue-400 dark:border-blue-800'
    ),
    messages.SUCCESS: (
        'text-green-800 border border-green-300 bg-green-50 '
        'dark:text-green-400 dark:border-green-800'
    ),
    messages.WARNING: (
        'text-yellow-800 border border-yellow-300 bg-yellow-50 '
        'dark:text-yellow-300 dark:border-yellow-800'
    ),
    messages.ERROR: (
        'text-red-800 border border-red-300 bg-red-50 '
        'dark:text-red-400 dark:border-red-800'
    ),
}

# --- S3 Storage Configuration ---

# Uses django-storages and boto3 to store files in S3 (MinIO).
DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'

AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY')
AWS_STORAGE_BUCKET_NAME = os.environ.get('AWS_STORAGE_BUCKET_NAME')
AWS_S3_ENDPOINT_URL = os.environ.get('AWS_S3_ENDPOINT_URL')
PUBLIC_URL = os.environ.get('PUBLIC_URL')
AWS_S3_CUSTOM_DOMAIN = f'{AWS_S3_ENDPOINT_URL}/{AWS_STORAGE_BUCKET_NAME}'
AWS_S3_REGION_NAME = os.environ.get('AWS_S3_REGION_NAME')
AWS_S3_ADDRESSING_STYLE = 'path'
AWS_S3_SIGNATURE_VERSION = "s3v4"
AWS_S3_FILE_OVERWRITE = False
AWS_DEFAULT_ACL = None

# Maximum number of files allowed in a single multipart upload.
DATA_UPLOAD_MAX_NUMBER_FILES = 1000

# --- Celery Configuration ---

# Configures Celery to use Redis as the message broker and result backend.
CELERY_BROKER_URL = 'redis://redis:6379/0'
CELERY_RESULT_BACKEND = 'redis://redis:6379/0'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes.
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60  # 25 minutes.

# --- Logging Configuration ---

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name} - {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
        'simple': {
            'format': '[{asctime}] {levelname} - {message}',
            'style': '{',
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
    },
    'handlers': {
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
        'user_project_db': {
            'level': 'INFO',
            'class': 'apps.logs.handlers.DatabaseLogHandler',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': True,
        },
        'apps': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'user_logs': {
            'handlers': ['console', 'user_project_db'],
            'level': 'INFO',
            'propagate': False,
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
}
