import os
import secrets
import string
import base64
import sys
from cryptography.fernet import Fernet

def generate_password(length=32):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))

def generate_secret_key():
    return secrets.token_urlsafe(50)

def generate_kms_key():
    return base64.b64encode(os.urandom(32)).decode()

def generate_fernet_key():
    return Fernet.generate_key().decode()

def setup_env():
    template_path = ".env.template"
    env_path = ".env"
    
    if not os.path.exists(template_path):
        print(f"❌ Error: {template_path} not found.")
        sys.exit(1)

    existing_env = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    existing_env[key.strip()] = value.strip()

    new_env_lines = []
    
    with open(template_path, "r") as f:
        template_lines = f.readlines()

    print("🚀 Starting environment setup...")

    for line in template_lines:
        original_line = line.rstrip()
        if not original_line or original_line.startswith("#") or "=" not in original_line:
            new_env_lines.append(original_line)
            continue

        key, template_value = original_line.split("=", 1)
        key = key.strip()
        template_value = template_value.strip()
        
        current_value = existing_env.get(key)
        
        # If value is already set and not a placeholder, keep it
        if current_value and not current_value.startswith("replace-with-"):
            new_env_lines.append(f"{key}={current_value}")
            continue

        # Auto-generation logic
        if key == "SECRET_KEY":
            val = generate_secret_key()
            print(f"✨ Generated {key}")
        elif "PASSWORD" in key or key.endswith("_PASS"):
            val = generate_password()
            print(f"✨ Generated {key}")
        elif key == "MINIO_KMS_SECRET_KEY":
            val = f"medswarmhub:{generate_kms_key()}"
            print(f"✨ Generated {key}")
        elif key == "FERNET_KEYS":
            val = generate_fernet_key()
            print(f"✨ Generated {key}")
        elif key == "BACKUP_ENCRYPTION_KEY":
            val = generate_fernet_key()
            print(f"✨ Generated {key}")
        elif key == "HOST_PROJECT_PATH":
            default_path = os.getcwd()
            val = input(f"❓ Enter {key} [{default_path}]: ").strip() or default_path
        elif template_value.startswith("replace-with-") or not template_value:
            # Prompt for other "replace-with" values
            val = input(f"❓ Enter value for {key} ({template_value}): ").strip()
            if not val:
                val = template_value
        else:
            # Keep template default if it's not a placeholder
            val = template_value

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
