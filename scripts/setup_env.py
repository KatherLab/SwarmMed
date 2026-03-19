"""Environment setup script for the MedSwarmHub platform.

This script automates the creation and configuration of the `.env` file from
`.env.template`. It handles the generation of secure random passwords, Django
secret keys, and cryptographic keys while prompting the user for necessary
manual configuration values.
"""

import base64
import os
import secrets
import string
import sys

from cryptography.fernet import Fernet

# Help dictionary providing descriptions and examples for environment variables
ENV_HELP = {
    "SECRET_KEY": {
        "desc": "Django's secret key used for cryptographic signing.",
        "example": "django-insecure-xyz123...",
    },
    "DEBUG": {
        "desc": "Enable or disable Django's debug mode with True (False for production).",
        "example": "False",
    },
    "DJANGO_ALLOWED_HOSTS": {
        "desc": "A comma-separated list of host/domain names this site can serve.",
        "example": "localhost,127.0.0.1,192.168.33.107,100.127.11.1",
    },
    "DJANGO_CSRF_TRUSTED_ORIGINS": {
        "desc": "A list of trusted origins for Unsafe requests (e.g. POST).",
        "example": "http://localhost:8000,http://localhost:5085,https://localhost:5085,https://100.127.11.1:5085",
    },
    "POSTGRES_PASSWORD": {
        "desc": "The password for the PostgreSQL database connection.",
        "example": "secure_db_pass_123",
    },
    "REDIS_PASSWORD": {
        "desc": "The password for the Redis cache/task broker.",
        "example": "redis_secure_pass_789",
    },
    "MINIO_ROOT_PASSWORD": {
        "desc": "The root administrator password for MinIO storage.",
        "example": "minio_secret_pass_456",
    },
    "MINIO_KMS_SECRET_KEY": {
        "desc": "Key Management Service secret key for MinIO encryption.",
        "example": "medswarmhub:base64_encoded_key",
    },
    "AWS_S3_REGION_NAME": {
        "desc": "The region name for S3 storage (often us-east-1 for MinIO).",
        "example": "eu-central-1",
    },
    "PUBLIC_URL": {
        "desc": "The public-facing URL for accessing stored files.",
        "example": "https://100.127.11.1:9000",
    },
    "PRIVACY_CONTROLLER_NAME": {
        "desc": "Legal name of the organization controlling the data.",
        "example": "KatherLab",
    },
    "PRIVACY_CONTROLLER_ADDRESS": {
        "desc": "Physical address of the organization.",
        "example": "123 Tech Lane, San Francisco, CA",
    },
    "PRIVACY_CONTACT_EMAIL": {
        "desc": "Primary email for privacy-related inquiries.",
        "example": "privacy@medswarmhub.org",
    },
    "PRIVACY_DPO_EMAIL": {
        "desc": "Email address for the Data Protection Officer.",
        "example": "dpo@medswarmhub.org",
    },
    "PRIVACY_DPO_ADDRESS": {
        "desc": "Physical address for the Data Protection Officer.",
        "example": "456 Compliance Ave, Berlin, Germany",
    },
    "PRIVACY_HOSTING_PROVIDER": {
        "desc": "Description of where the platform is hosted.",
        "example": "Self-hosted",
    },
    "PRIVACY_DATA_REGION": {
        "desc": "The geographic region where user data is stored.",
        "example": "EU (Germany)",
    },
    "EMAIL_HOST": {
        "desc": "SMTP server hostname for sending emails.",
        "example": "smtp.gmail.com",
    },
    "EMAIL_PORT": {
        "desc": "SMTP server port (usually 587 for TLS).",
        "example": "587",
    },
    "EMAIL_HOST_USER": {
        "desc": "The username for the SMTP email server.",
        "example": "alerts@example.com",
    },
    "EMAIL_HOST_PASSWORD": {
        "desc": "The password or App Password for the SMTP email server.",
        "example": "abcd-efgh-ijkl-mnop",
    },
    "FERNET_KEYS": {
        "desc": "Comma-separated Fernet keys for encrypting data at rest.",
        "example": "base64_key1,base64_key2",
    },
    "BACKUP_ENCRYPTION_KEY": {
        "desc": "The Fernet key specifically for database/media backups.",
        "example": "base64_backup_key",
    },
    "HOST_PROJECT_PATH": {
        "desc": "Absolute path to the project on your HOST machine (for Docker mounts).",
        "example": "/opt/MedSwarmHub",
    },
    "MEDSWARMHUB_HOSTNAME": {
        "desc": "Custom hostname for the platform deployment.",
        "example": "pc1.tud",
    },
    "ALLOWED_EXTENSIONS": {
        "desc": "Comma-separated list of allowed file extensions for data upload.",
        "example": ".csv,.txt,.json,.parquet,.npy,.npz,.h5,.pt,.pth,.dcm,.nii,.nii.gz,.jpg,.jpeg,.png,.bmp,.gif,.pdf",
    },
    "DOCKER_HOST_IP": {
        "desc": "The IP address of the host machine as seen from the containers, or for binding ports to the host.",
        "example": "127.0.0.1 (Mac/Win) or 172.17.0.1 (Linux)",
    },
}


def generate_password(length=32):
    """Generates a secure random password.

    Args:
        length (int): The number of characters in the password. Defaults to 32.

    Returns:
        str: A secure random alphanumeric password.
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_secret_key():
    """Generates a Django-compatible secret key.

    Returns:
        str: A URL-safe text string containing 50 random bytes.
    """
    return secrets.token_urlsafe(50)


def generate_kms_key():
    """Generates a base64 encoded random key for KMS.

    Returns:
        str: A base64 encoded string of 32 random bytes.
    """
    return base64.b64encode(os.urandom(32)).decode()


def generate_fernet_key():
    """Generates a Fernet encryption key.

    Returns:
        str: A URL-safe base64-encoded 32-byte key.
    """
    return Fernet.generate_key().decode()


def print_help(key):
    """Prints help information for a specific environment variable.

    Args:
        key (str): The environment variable key to look up in ENV_HELP.
    """
    help_info = ENV_HELP.get(key)
    if help_info:
        print(f"\n📝 {help_info['desc']}")
        print(f"💡 Example: {help_info['example']}")


def setup_env():
    """Orchestrates the environment setup process.

    Reads `.env.template`, prompts for missing values, generates keys, and
    saves the final configuration to `.env`.
    """
    template_path = ".env.template"
    env_path = ".env"

    if not os.path.exists(template_path):
        print(f"❌ Error: {template_path} not found.")
        sys.exit(1)

    # resolved_env will store the final values for each key
    resolved_env = {}

    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    resolved_env[key.strip()] = value.strip()

    new_env_lines = []

    with open(template_path) as f:
        template_lines = f.readlines()

    print("🚀 Starting environment setup...")

    for line in template_lines:
        original_line = line.rstrip()
        if (
            not original_line
            or original_line.startswith("#")
            or "=" not in original_line
        ):
            new_env_lines.append(original_line)
            continue

        key, template_value = original_line.split("=", 1)
        key = key.strip()
        template_value = template_value.strip()

        current_value = resolved_env.get(key)

        # If value is already set and not a placeholder or 'same-as', keep it
        if (
            current_value
            and not current_value.startswith("replace-with-")
            and not current_value.startswith("same-as-")
        ):
            val = current_value
        elif template_value.startswith("same-as-"):
            target_key = template_value.replace("same-as-", "").strip()
            val = resolved_env.get(target_key)
            if not val:
                print(
                    f"⚠️ Warning: {key} depends on {target_key}, but {target_key} is not yet defined."
                )
                val = template_value  # Keep it to potentially resolve later or fail
            else:
                print(f"🔗 Linked {key} to {target_key}")
        # Auto-generation logic
        elif key == "SECRET_KEY":
            val = generate_secret_key()
            print(f"✨ Generated {key}")
        elif key == "DOCKER_HOST_IP":
            if sys.platform == "darwin":
                val = "127.0.0.1"
            elif sys.platform.startswith("linux"):
                val = "172.17.0.1"
            elif sys.platform == "win32":
                val = "127.0.0.1"
            else:
                val = "127.0.0.1"
            print(f"✨ Auto-detected {key} for {sys.platform}: {val}")
        elif ("PASSWORD" in key or key.endswith("_PASS")) and key != "EMAIL_HOST_PASSWORD":
            val = generate_password()
            print(f"✨ Generated {key}")
        elif key == "MINIO_KMS_SECRET_KEY":
            val = f"medswarmhub:{generate_kms_key()}"
            print(f"✨ Generated {key}")
        elif key == "FERNET_KEYS" or key == "BACKUP_ENCRYPTION_KEY":
            val = generate_fernet_key()
            print(f"✨ Generated {key}")
        elif (
            key in ENV_HELP
            or template_value.startswith("replace-with-")
            or not template_value
        ):
            # Prompt for values in ENV_HELP or marked as replace-with
            print_help(key)
            default_val = template_value
            # If it's a "replace-with" placeholder, don't use it as the default text in []
            display_default = (
                f" [{default_val}]" if not default_val.startswith("replace-with-") else ""
            )

            val = input(f"❓ Enter {key}{display_default}: ").strip()
            if not val:
                val = default_val
        else:
            # Keep template default if it's not a placeholder and not in ENV_HELP
            val = template_value

        resolved_env[key] = val
        new_env_lines.append(f"{key}={val}")

    with open(env_path, "w") as f:
        f.write("\n".join(new_env_lines) + "\n")

    print(f"\n✅ Environment file '{env_path}' has been updated.")


if __name__ == "__main__":
    try:
        setup_env()
    except KeyboardInterrupt:
        print("\n\n⚠️ Setup interrupted by user.")
        sys.exit(1)
