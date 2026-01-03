"""
Django settings for the SwarmCloud project.
This file contains the configuration for the entire web application, including
database connections, security keys, installed apps, and middleware.
"""

import os
import secrets
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

# DEBUG mode should be True for development and False for production.
DEBUG = str2bool(os.environ.get("DEBUG", "False"))

# The SECRET_KEY is used for cryptographic signing.
# In production, this MUST be set in the environment.
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        # Only generate a random key if we are in DEBUG mode.
        alphabet = string.ascii_letters + string.digits
        SECRET_KEY = "".join(secrets.choice(alphabet) for _ in range(50))
    else:
        raise ValueError("SECRET_KEY environment variable is not set and DEBUG is False.")

# Fernet Encryption Keys (for django-fernet-fields)
# In production, this MUST be set in the environment as a comma-separated list of keys.
FERNET_KEYS = os.environ.get("FERNET_KEYS", SECRET_KEY).split(",")
FERNET_USE_HKDF = True

# Backup Encryption Key
BACKUP_ENCRYPTION_KEY = os.environ.get("BACKUP_ENCRYPTION_KEY", SECRET_KEY)

# ALLOWED_HOSTS defines which domain names can access this server.
# It should be restricted to your production domains.
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# CSRF_TRUSTED_ORIGINS is required for cross-site request forgery protection
# when running on specific domains or ports.
CSRF_TRUSTED_ORIGINS = os.environ.get(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "http://localhost:8000,http://localhost:5085,http://127.0.0.1:8000,http://127.0.0.1:5085",
).split(",")

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
    "apps.core",
    "apps.users",
    "apps.project",
    "apps.data",
    "apps.network",
    "apps.training",
    "apps.results",
    "apps.logs",
    "apps.communication",
    # Third-party extensions.
    "storages",
    "axes",
    # Multi-Factor Authentication
    "django_otp",
    "django_otp.plugins.otp_static",
    "django_otp.plugins.otp_totp",
    "two_factor",
    # "two_factor.plugins.phonenumber",  # Optional: Phone number support
]

# Middleware components process requests and responses globally.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # For static file serving.
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",  # Multi-Factor Authentication
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.logs.context.RequestContextMiddleware",  # Custom context logging.
    "axes.middleware.AxesMiddleware",  # Brute-force protection
    "apps.users.middleware.MFAEnforcementMiddleware",  # Strict MFA enforcement
    "apps.users.middleware.GDPRRestrictionMiddleware",  # GDPR Right to Restriction
]

if DEBUG:
    INSTALLED_APPS.append("debug_toolbar")
    MIDDLEWARE.insert(
        MIDDLEWARE.index("django.middleware.common.CommonMiddleware") + 1,
        "debug_toolbar.middleware.DebugToolbarMiddleware",
    )

# The main URL configuration for the project.
ROOT_URLCONF = "core.urls"

# Location of HTML template files.
UI_TEMPLATES = os.path.join(BASE_DIR, "templates")

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
    "default": {
        "ENGINE": os.getenv("DB_ENGINE"),
        "NAME": os.getenv("DB_NAME"),
        "USER": os.getenv("DB_USER"),
        "PASSWORD": os.getenv("DB_PASS"),
        "HOST": os.getenv("DB_HOST"),
        "PORT": os.getenv("DB_PORT"),
        "OPTIONS": {
            "sslmode": "verify-full" if not DEBUG else "require",
            "sslrootcert": "/usr/local/share/ca-certificates/internal-ca.crt",
        },
    },
}

# --- Password Validation ---

# Standard Django rules to ensure users choose strong passwords.
# Enhanced for HIPAA compliance (Minimum length 12, complexity check).
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {
            "min_length": 12,
        }
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"
    },
]

# --- Internationalization ---

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static and Media Files ---

# Static files (CSS, JavaScript, Images) for the UI.
STATIC_URL = "static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")
STATICFILES_DIRS = (os.path.join(BASE_DIR, "static"),)

# Media files (uploaded by users, such as CSV datasets).
MEDIA_URL = "media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")

# Default primary key field type for models.
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Authentication and Email ---

# --- Authentication Backends & Security ---

AUTHENTICATION_BACKENDS = [
    # AxesStandaloneBackend should be the first backend in the AUTHENTICATION_BACKENDS list.
    'axes.backends.AxesStandaloneBackend',
    # Django ModelBackend is the default authentication backend.
    'django.contrib.auth.backends.ModelBackend',
]

# --- Axes Configuration (Brute Force Protection) ---

# Block login after 5 failed attempts
AXES_FAILURE_LIMIT = 5
# Cooloff period: 1 hour
AXES_COOLOFF_TIME = 1
# Lock out based on user and IP combination
AXES_LOCK_OUT_PARAMETERS = ["username", "ip_address"]
# Reset failed attempts on success
AXES_RESET_ON_SUCCESS = True

# Enforce MFA Login Flow
LOGIN_URL = "two_factor:login"
LOGIN_REDIRECT_URL = "/"

# Email configuration for password resets and notifications.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.environ.get("EMAIL_HOST")
EMAIL_PORT = os.environ.get("EMAIL_PORT")
EMAIL_USE_TLS = str2bool(os.environ.get("EMAIL_USE_TLS", "False"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD")

# --- HIPAA Compliance: Session Management ---
# SESSION_COOKIE_HTTPONLY is True for better security.
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True

# Automatic Logoff (HIPAA Technical Safeguard: 164.312(a)(2)(iii))
# Expire session on browser close
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
# Session timeout after 30 minutes of inactivity (1800 seconds)
SESSION_COOKIE_AGE = 1800
# Save the session on every request to update the expiration time
SESSION_SAVE_EVERY_REQUEST = True

# Mapping Django message levels to Tailwind CSS classes.
MESSAGE_TAGS = {
    messages.INFO: (
        "text-blue-800 border border-blue-300 bg-blue-50 "
        "dark:text-blue-400 dark:border-blue-800"
    ),
    messages.SUCCESS: (
        "text-green-800 border border-green-300 bg-green-50 "
        "dark:text-green-400 dark:border-green-800"
    ),
    messages.WARNING: (
        "text-yellow-800 border border-yellow-300 bg-yellow-50 "
        "dark:text-yellow-300 dark:border-yellow-800"
    ),
    messages.ERROR: (
        "text-red-800 border border-red-300 bg-red-50 "
        "dark:text-red-400 dark:border-red-800"
    ),
}

# --- S3 Storage Configuration ---

# Uses django-storages and boto3 to store files in S3 (MinIO).
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
if not DEBUG and (not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY):
    raise ValueError("AWS credentials MUST be set in environment when DEBUG is False.")

# For development only: allow local default credentials but do not overwrite
# production environment variables. This prevents accidental use of
# hard-coded defaults when running with DEBUG=False.
if DEBUG:
    AWS_ACCESS_KEY_ID = AWS_ACCESS_KEY_ID or "minioadmin"
    AWS_SECRET_ACCESS_KEY = AWS_SECRET_ACCESS_KEY or "minioadmin"
AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME", "swarmcloud")
AWS_S3_ENDPOINT_URL = os.environ.get("AWS_S3_ENDPOINT_URL", "https://minio:9000")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://localhost:9000")
AWS_S3_CUSTOM_DOMAIN = f"{AWS_S3_ENDPOINT_URL}/{AWS_STORAGE_BUCKET_NAME}"
AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME", "eu-central-1")
AWS_S3_ADDRESSING_STYLE = "path"
AWS_S3_SIGNATURE_VERSION = "s3v4"
AWS_S3_FILE_OVERWRITE = False
AWS_DEFAULT_ACL = None
# Enforce Server-Side Encryption (SSE-S3)
AWS_S3_OBJECT_PARAMETERS = {
    "ServerSideEncryption": "AES256",
}

STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG else "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Prevent collectstatic from failing on missing optional files (like source maps)
WHITENOISE_MANIFEST_STRICT = False

# --- Path Translation for Docker Sandbox ---
# If running inside Docker, we need the host's absolute path to the project
# to correctly mount volumes into the sandbox containers.
HOST_PROJECT_PATH = os.environ.get("HOST_PROJECT_PATH", str(BASE_DIR))
PROJECT_TEMP_DIR = os.path.join(BASE_DIR, "tmp")

# Ensure the project-local temp directory exists
os.makedirs(PROJECT_TEMP_DIR, exist_ok=True)

# Keep this for backward compatibility with some apps or older Django versions
DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"

# Maximum number of files allowed in a single multipart upload.
DATA_UPLOAD_MAX_NUMBER_FILES = 1000

# --- HIPAA Compliance: Data Retention and Backup ---
# Retention period for database backups (in days)
BACKUP_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", 30))
# Retention period for PHI and related records (in days). Default: 6 years (2190 days)
DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", 2190))
# Retention period for standard security logs (in days). Default: 1 year (365 days)
SECURITY_LOG_RETENTION_DAYS = int(os.environ.get("SECURITY_LOG_RETENTION_DAYS", 365))
# Period after which IP addresses are anonymized (in days). Default: 90 days
IP_ANONYMIZATION_DAYS = int(os.environ.get("IP_ANONYMIZATION_DAYS", 90))

# --- Celery Configuration ---

# Configures Celery to use Redis as the message broker and result backend.
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD")
if not REDIS_PASSWORD and not DEBUG:
    raise ValueError("REDIS_PASSWORD MUST be set in environment when DEBUG is False.")

# For development only: permit an empty password locally but do not
# overwrite a missing production secret.
if DEBUG:
    REDIS_PASSWORD = REDIS_PASSWORD or ""

REDIS_HOST = "redis"
REDIS_PORT = 6379
CA_CERT_PATH = "/usr/local/share/ca-certificates/internal-ca.crt"

CELERY_BROKER_URL = f"rediss://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0?ssl_cert_reqs=required&ssl_ca_certs={CA_CERT_PATH}"
CELERY_RESULT_BACKEND = f"rediss://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0?ssl_cert_reqs=required&ssl_ca_certs={CA_CERT_PATH}"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes.
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60  # 25 minutes.

# Automation Schedule (Celery Beat)
from celery.schedules import crontab

CELERY_BEAT_SCHEDULE = {
    'daily-secure-backup': {
        'task': 'apps.logs.tasks.scheduled_backup',
        'schedule': crontab(hour=2, minute=0),  # Daily at 2:00 AM UTC
    },
    'daily-data-retention-purge': {
        'task': 'apps.logs.tasks.purge_expired_data',
        'schedule': crontab(hour=3, minute=0),  # Daily at 3:00 AM UTC
    },
    'daily-ip-anonymization': {
        'task': 'apps.logs.tasks.anonymize_security_logs',
        'schedule': crontab(hour=4, minute=0),  # Daily at 4:00 AM UTC
    },
    'hourly-emergency-access-cleanup': {
        'task': 'apps.logs.tasks.revoke_emergency_access',
        'schedule': crontab(minute=0),  # Every hour
    },
}

# --- Security Hardening for Production ---
if not DEBUG:
    SECURE_SSL_REDIRECT = str2bool(os.environ.get("SECURE_SSL_REDIRECT", "True"))
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --- Logging Configuration ---

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} - {message}",
            "style": "{",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "simple": {
            "format": "[{asctime}] {levelname} - {message}",
            "style": "{",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
        "user_project_db": {
            "level": "INFO",
            "class": "apps.logs.handlers.DatabaseLogHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": True,
        },
        "apps": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "user_logs": {
            "handlers": ["console", "user_project_db"],
            "level": "INFO",
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
}
