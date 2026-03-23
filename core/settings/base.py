"""Django settings for the MedSwarmHub project.
This file contains the configuration for the entire web application, including
database connections, security keys, installed apps, and middleware.
"""

import os
import secrets
import string
from pathlib import Path
from urllib.parse import urlparse

from celery.schedules import crontab
from django.contrib import messages
from dotenv import load_dotenv
from str2bool import str2bool

# Load environment variables from a .env file into os.environ.
# This is used for sensitive information like passwords and API keys.
load_dotenv()

# Build paths inside the project like this: BASE_DIR / 'subdir'.
# BASE_DIR points to the root directory of the project.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

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
        raise ValueError(
            "SECRET_KEY environment variable is not set and DEBUG is False."
        )

# Fernet Encryption Keys (for django-fernet-fields)
# In production, this MUST be set in the environment as a comma-separated list of keys.
# We ensure it's distinct from SECRET_KEY to prevent multi-layer compromise.
_fernet_keys_raw = os.environ.get("FERNET_KEYS")
if not _fernet_keys_raw:
    if DEBUG:
        # Fallback to SECRET_KEY only in development for convenience.
        FERNET_KEYS = [SECRET_KEY]
    else:
        # Critical Security: Force dedicated encryption keys in production.
        raise ValueError(
            "FERNET_KEYS environment variable is not set and DEBUG is False. "
            "PHI/PII protection requires a dedicated encryption key distinct from SECRET_KEY."
        )
else:
    FERNET_KEYS = _fernet_keys_raw.split(",")
FERNET_USE_HKDF = True

# Backup Encryption Key
BACKUP_ENCRYPTION_KEY = os.environ.get("BACKUP_ENCRYPTION_KEY")
if not BACKUP_ENCRYPTION_KEY:
    if DEBUG:
        BACKUP_ENCRYPTION_KEY = SECRET_KEY
    else:
        raise ValueError(
            "BACKUP_ENCRYPTION_KEY MUST be set in environment when DEBUG is False."
        )
elif not DEBUG and BACKUP_ENCRYPTION_KEY == SECRET_KEY:
    raise ValueError(
        "BACKUP_ENCRYPTION_KEY MUST be different from SECRET_KEY when DEBUG is False."
    )

# ALLOWED_HOSTS defines which domain names can access this server.
# It should be restricted to your production domains.
ALLOWED_HOSTS = os.environ.get(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1"
).split(",")

# CSRF_TRUSTED_ORIGINS is required for cross-site request forgery protection
# when running on specific domains or ports.
CSRF_TRUSTED_ORIGINS = os.environ.get(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "http://localhost:8000,http://localhost:5085,http://127.0.0.1:8000,http://127.0.0.1:5085",
).split(",")

# IPs allowed to see the Django Debug Toolbar.
INTERNAL_IPS = ["127.0.0.1"]

# Allowed file extensions for data upload.
ALLOWED_EXTENSIONS = set(os.environ.get(
    "ALLOWED_EXTENSIONS",
    ".csv,.txt,.json,,.npy,.npz,.h5,.pt,.pth,.dcm,.nii,.nii.gz,.jpg,.jpeg,.png,.bmp,.gif,.pdf"
).split(","))

# --- Application Definition ---

# List of Django apps enabled for this project.
INSTALLED_APPS = [
    "unfold",  # before django.contrib.admin
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "unfold.contrib.inlines",
    # Core Django apps.
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Custom project-specific apps.
    "home",
    "common",
    "users",
    "project",
    "data",
    "network",
    "training",
    "results",
    "logs",
    "communication",
    "backup",
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
    "logs.context.RequestContextMiddleware",  # Custom context logging.
    "logs.middleware.AuditLogMiddleware",  # Audit logging for write requests.
    "axes.middleware.AxesMiddleware",  # Brute-force protection
    "common.middleware.GDPRRestrictionMiddleware",  # GDPR Right to Restriction
    "common.middleware.LegalAcceptanceMiddleware",  # Ensure legal terms are accepted
]

# The main URL configuration for the project.
ROOT_URLCONF = "core.urls"

# Location of HTML template files.
UI_TEMPLATES = os.path.join(BASE_DIR, "templates")

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [UI_TEMPLATES],
        "APP_DIRS": False,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Custom context processor for unread message counts.
                "communication.context_processors.unread_messages",
                # Custom context processor for common legal and contact information.
                "common.context_processors.legal_and_contact_info",
            ],
            # To be overridden in dev/prod
            "loaders": [
                "django.template.loaders.filesystem.Loader",
                "django.template.loaders.app_directories.Loader",
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
        "CONN_MAX_AGE": 600,  # Persistent connections (10 mins)
        "CONN_HEALTH_CHECKS": True,  # Validate connections before reuse
        "OPTIONS": {
            "sslmode": "require",
            "sslrootcert": "/usr/local/share/ca-certificates/internal-ca.crt",
        },
    },
}

# --- Caching Configuration ---

# Configures Redis connection parameters used by CACHES and Celery.
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD")
if not REDIS_PASSWORD and not DEBUG:
    raise ValueError(
        "REDIS_PASSWORD MUST be set in environment when DEBUG is False."
    )

# For development only: permit an empty password locally but do not
# overwrite a missing production secret.
if DEBUG:
    REDIS_PASSWORD = REDIS_PASSWORD or ""

REDIS_HOST = "redis"
REDIS_PORT = 6379
CA_CERT_PATH = "/usr/local/share/ca-certificates/internal-ca.crt"

# Configures Django to use Redis for caching.
# This improves performance for frequently accessed data and sessions.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.environ.get(
            "REDIS_CACHE_URL",
            f"rediss://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/1?ssl_cert_reqs=required&ssl_ca_certs={CA_CERT_PATH}",
        ),
        "TIMEOUT": 300,  # Default cache timeout: 5 minutes
        "OPTIONS": {},
    }
}

# --- Session Management ---

# Store sessions in the cache instead of the database for better performance.
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

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
        },
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"
    },
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
    "axes.backends.AxesStandaloneBackend",
    # Django ModelBackend is the default authentication backend.
    "django.contrib.auth.backends.ModelBackend",
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
LOGIN_URL = "users:two_factor:login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "users:two_factor:login"
TWO_FACTOR_URL_NAMESPACE = "users:two_factor"

# Explicitly set namespaced URLs for 2FA flow
TWO_FACTOR_LOGIN_URL = "users:two_factor:login"
TWO_FACTOR_SETUP_SUCCESS_URL = "users:two_factor:setup_complete"
TWO_FACTOR_BACKUP_TOKENS_URL = "users:two_factor:backup_tokens"
TWO_FACTOR_PROFILE_URL = "users:two_factor:profile"

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
    raise ValueError(
        "AWS credentials MUST be set in environment when DEBUG is False."
    )

# For development only: allow local default credentials but do not overwrite
# production environment variables. This prevents accidental use of
# hard-coded defaults when running with DEBUG=False.
if DEBUG:
    AWS_ACCESS_KEY_ID = AWS_ACCESS_KEY_ID or "minioadmin"
    AWS_SECRET_ACCESS_KEY = AWS_SECRET_ACCESS_KEY or "minioadmin"
AWS_STORAGE_BUCKET_NAME = os.environ.get(
    "AWS_STORAGE_BUCKET_NAME", "medswarmhub"
)
AWS_S3_ENDPOINT_URL = os.environ.get(
    "AWS_S3_ENDPOINT_URL", "https://minio:9000"
)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://localhost:9000")
AWS_S3_CUSTOM_DOMAIN = (
    f"{urlparse(PUBLIC_URL).netloc}/{AWS_STORAGE_BUCKET_NAME}"
)
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
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
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
SECURITY_LOG_RETENTION_DAYS = int(
    os.environ.get("SECURITY_LOG_RETENTION_DAYS", 365)
)
# Period after which IP addresses are anonymized (in days). Default: 90 days
IP_ANONYMIZATION_DAYS = int(os.environ.get("IP_ANONYMIZATION_DAYS", 90))

# --- Privacy Policy Runtime Values ---
# These values populate the in-app privacy policy so deployments do not ship
# unresolved placeholders for controller, DPO, or retention metadata.

# --- Global Organization and Contact Information ---
ORGANIZATION_NAME = os.environ.get("ORGANIZATION_NAME", "Not specified")
ORGANIZATION_STREET = os.environ.get("ORGANIZATION_STREET", "Not specified")
ORGANIZATION_ZIP_CITY = os.environ.get("ORGANIZATION_ZIP_CITY", "Not specified")
ORGANIZATION_COUNTRY = os.environ.get("ORGANIZATION_COUNTRY", "Not specified")
ORGANIZATION_WEBSITE = os.environ.get("ORGANIZATION_WEBSITE", "Not specified")
REPRESENTATIVE_NAME = os.environ.get("REPRESENTATIVE_NAME", "Not specified")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "Not specified")
CONTACT_PHONE = os.environ.get("CONTACT_PHONE", "Not specified")
EDITORIAL_RESPONSIBLE_NAME = os.environ.get("EDITORIAL_RESPONSIBLE_NAME", REPRESENTATIVE_NAME)
EDITORIAL_RESPONSIBLE_ADDRESS = os.environ.get(
    "EDITORIAL_RESPONSIBLE_ADDRESS", 
    f"{ORGANIZATION_STREET}, {ORGANIZATION_ZIP_CITY}, {ORGANIZATION_COUNTRY}"
)

PRIVACY_CONTROLLER_NAME = os.environ.get(
    "PRIVACY_CONTROLLER_NAME", ORGANIZATION_NAME
)
PRIVACY_CONTROLLER_ADDRESS = os.environ.get(
    "PRIVACY_CONTROLLER_ADDRESS", 
    f"{ORGANIZATION_STREET}, {ORGANIZATION_ZIP_CITY}, {ORGANIZATION_COUNTRY}"
)
PRIVACY_CONTACT_EMAIL = os.environ.get(
    "PRIVACY_CONTACT_EMAIL", CONTACT_EMAIL
)
PRIVACY_DPO_EMAIL = os.environ.get("PRIVACY_DPO_EMAIL", PRIVACY_CONTACT_EMAIL)
PRIVACY_DPO_ADDRESS = os.environ.get(
    "PRIVACY_DPO_ADDRESS", PRIVACY_CONTROLLER_ADDRESS
)
PRIVACY_HOSTING_PROVIDER = os.environ.get(
    "PRIVACY_HOSTING_PROVIDER", "Not specified"
)
PRIVACY_DATA_REGION = os.environ.get(
    "PRIVACY_DATA_REGION", "Not specified"
)
ACCOUNT_ERASURE_GRACE_DAYS = int(
    os.environ.get("ACCOUNT_ERASURE_GRACE_DAYS", 30)
)

# --- Celery Configuration ---

# Configures Celery to use Redis as the message broker and result backend.
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD")
if not REDIS_PASSWORD and not DEBUG:
    raise ValueError(
        "REDIS_PASSWORD MUST be set in environment when DEBUG is False."
    )

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
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_POOL_LIMIT = 10  # Limit Redis connection pool size

# Automation Schedule (Celery Beat)

CELERY_BEAT_SCHEDULE = {
    "daily-secure-backup": {
        "task": "backup.tasks.scheduled_backup",
        "schedule": crontab(hour=2, minute=0),  # Daily at 2:00 AM UTC
    },
    "daily-data-retention-purge": {
        "task": "logs.tasks.purge_expired_data",
        "schedule": crontab(hour=3, minute=0),  # Daily at 3:00 AM UTC
    },
    "daily-ip-anonymization": {
        "task": "logs.tasks.anonymize_security_logs",
        "schedule": crontab(hour=4, minute=0),  # Daily at 4:00 AM UTC
    },
    "hourly-emergency-access-cleanup": {
        "task": "logs.tasks.revoke_emergency_access",
        "schedule": crontab(minute=0),  # Every hour
    },
    "monitor-training-jobs": {
        "task": "training.tasks.monitor_training_jobs",
        "schedule": 60.0,  # Every 60 seconds (reduced from 30s)
    },
    "broadcast-network-statuses": {
        "task": "network.tasks.broadcast_all_network_statuses",
        "schedule": 30.0,  # Every 30 seconds (reduced from 10s)
    },
}

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
            "class": "logs.handlers.DatabaseLogHandler",
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

# --- Unfold Admin Configuration ---

UNFOLD = {
    "SITE_TITLE": "MedSwarmHub Admin",
    "SITE_HEADER": "MedSwarmHub Admin",
    "SITE_SYMBOL": "cloud",  # icon from Material Symbols
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "DASHBOARD_CALLBACK": "apps.common.views.dashboard_callback",
    "COLORS": {
        "primary": {
            "50": "250 252 255",
            "100": "240 247 255",
            "200": "186 220 255",
            "300": "133 193 255",
            "400": "28 140 255",
            "500": "0 112 240",
            "600": "0 101 216",
            "700": "0 84 180",
            "800": "0 67 144",
            "900": "0 55 118",
            "950": "0 31 66",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": "User Management",
                "items": [
                    {
                        "title": "Users",
                        "icon": "person",
                        "link": "/admin/auth/user/",
                    },
                    {
                        "title": "Groups",
                        "icon": "group",
                        "link": "/admin/auth/group/",
                    },
                    {
                        "title": "Profiles",
                        "icon": "contact_page",
                        "link": "/admin/users/profile/",
                    },
                ],
            },
            {
                "title": "Access Management",
                "items": [
                    {
                        "title": "Attempts",
                        "icon": "feedback",
                        "link": "/admin/axes/accessattempt/",
                    },
                    {
                        "title": "Failures",
                        "icon": "chat_error",
                        "link": "/admin/axes/accessfailurelog/",
                    },
                    {
                        "title": "Logins",
                        "icon": "login",
                        "link": "/admin/axes/accesslog/",
                    },
                ],
            },
            {
                "title": "Project Management",
                "items": [
                    {
                        "title": "Projects",
                        "icon": "folder",
                        "link": "/admin/project/project/",
                    },
                    {
                        "title": "Swarm Networks",
                        "icon": "hub",
                        "link": "/admin/network/swarmnetwork/",
                    },
                    {
                        "title": "Training Jobs",
                        "icon": "model_training",
                        "link": "/admin/training/trainingjob/",
                    },
                ],
            },
            {
                "title": "Data Management",
                "items": [
                    {
                        "title": "MInIO Database",
                        "icon": "data_table",
                        "link": "https://127.0.0.1:9001",
                    },
                    {
                        "title": "Validation Runs",
                        "icon": "fact_check",
                        "link": "/admin/data/validationrun/",
                    },
                    {
                        "title": "Visualization Runs",
                        "icon": "analytics",
                        "link": "/admin/data/visualizationrun/",
                    },
                ],
            },
            {
                "title": "Results Management",
                "items": [
                    {
                        "title": "Results",
                        "icon": "data_object",
                        "link": "/admin/results/trainingresult/",
                    },
                    {
                        "title": "Visualization Runs",
                        "icon": "analytics",
                        "link": "/admin/results/resultsvisualizationrun/",
                    },
                ],
            },
            {
                "title": "Communication Management",
                "items": [
                    {
                        "title": "Messages",
                        "icon": "chat",
                        "link": "/admin/communication/message/",
                    },
                    {
                        "title": "Board posts",
                        "icon": "comment",
                        "link": "/admin/communication/projectpost/",
                    },
                ],
            },
            {
                "title": "System Infrastructure",
                "items": [
                    {
                        "title": "Audit Logs",
                        "icon": "receipt_long",
                        "link": "/admin/logs/logentry/",
                    },
                    {
                        "title": "MFA Devices",
                        "icon": "security",
                        "link": "/admin/otp_totp/totpdevice/",
                    },
                ],
            },
        ],
    },
}
