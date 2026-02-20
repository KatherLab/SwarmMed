#!/usr/bin/env python3
import base64
import hashlib
import hmac
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

SECRET_DIR = Path(
    os.environ.get(
        "PGBOUNCER_SECRET_DIR",
        Path(__file__).parent.parent / ".secrets/pgbouncer",
    )
)


def ensure_secret_dir():
    SECRET_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRET_DIR, 0o700)
    return SECRET_DIR


def write_secret_file(path: Path, content: str):
    path.write_text(content)
    os.chmod(path, 0o600)
    return path


def generate_scram_hash(password, salt=None, iterations=4096):
    if salt is None:
        salt = os.urandom(16)
    else:
        if isinstance(salt, str):
            salt = base64.b64decode(salt)

    salted_password = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    client_key = hmac.new(
        salted_password, b"Client Key", hashlib.sha256
    ).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(
        salted_password, b"Server Key", hashlib.sha256
    ).digest()

    auth_str = f"SCRAM-SHA-256${iterations}:{base64.b64encode(salt).decode()}${base64.b64encode(stored_key).decode()}:{base64.b64encode(server_key).decode()}"
    return auth_str


def main():
    load_dotenv()
    db_user = os.environ.get("DB_USER")
    db_pass = os.environ.get("DB_PASS")
    db_name = os.environ.get("DB_NAME")
    db_host = os.environ.get("BACKEND_DB_HOST", "postgres")
    db_port = os.environ.get("BACKEND_DB_PORT", "5432")

    if not db_user or not db_pass or not db_name:
        raise RuntimeError(
            "DB_USER, DB_PASS, and DB_NAME must be set for PgBouncer setup"
        )

    if (
        db_pass == "swarmcloud"
        and os.environ.get("ALLOW_INSECURE_PGBOUNCER") != "1"
    ):
        raise RuntimeError(
            "Default database password detected. Refusing to write insecure PgBouncer config."
        )

    config_dir = ensure_secret_dir()

    # Generate userlist.txt
    print(f"Generating userlist.txt for user: {db_user}")
    scram_hash = generate_scram_hash(db_pass)
    userlist_content = f'"{db_user}" "{scram_hash}"\n'
    write_secret_file(config_dir / "userlist.txt", userlist_content)

    # Generate pgbouncer.ini
    print("Generating pgbouncer.ini")
    ini_content = f"""
[databases]
{db_name} = host={db_host} port={db_port} dbname={db_name} user={db_user} password={db_pass}

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 5432
unix_socket_dir = /tmp
auth_file = /etc/pgbouncer/userlist.txt
auth_type = scram-sha-256
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 25
min_pool_size = 10
reserve_pool_size = 5
ignore_startup_parameters = extra_float_digits
admin_users = {db_user}
server_idle_timeout = 600

# Allow SSL connections to the backend
server_tls_sslmode = require
server_tls_ca_file = /etc/pgbouncer/certs/ca.crt

# Allow SSL connections from clients
client_tls_sslmode = require
client_tls_key_file = /etc/pgbouncer/certs/pgbouncer.key
client_tls_cert_file = /etc/pgbouncer/certs/pgbouncer.crt
client_tls_ca_file = /etc/pgbouncer/certs/ca.crt
""".strip()
    write_secret_file(config_dir / "pgbouncer.ini", ini_content)
    print("PgBouncer configuration generated successfully.")

    # Reload PgBouncer
    print("Reloading PgBouncer...")
    pgbouncer_container = os.environ.get(
        "PGBOUNCER_CONTAINER_NAME", "pgbouncer"
    )
    try:
        subprocess.run(
            ["docker", "kill", "-s", "HUP", pgbouncer_container],
            check=True,
            capture_output=True,
            text=True,
        )
        print("PgBouncer reloaded.")
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.lower() if e.stderr else ""
        if "permission denied" in err_msg:
            print("Docker permission denied. Trying with sudo...")
            try:
                subprocess.run(
                    ["sudo", "docker", "kill", "-s", "HUP", pgbouncer_container],
                    check=True,
                )
                print("PgBouncer reloaded (via sudo).")
            except subprocess.CalledProcessError:
                print(
                    "Warning: Could not reload PgBouncer. If the container is running, please restart it manually."
                )
        elif "no such container" in err_msg:
            print("PgBouncer container not running (skipping reload).")
        else:
            print(f"Warning: Failed to reload PgBouncer: {e.stderr.strip()}")
    except FileNotFoundError:
         print("Docker command not found. Skipping PgBouncer reload.")


if __name__ == "__main__":
    main()
