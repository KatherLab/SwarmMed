#!/usr/bin/env python3
import os
import hashlib
import hmac
import base64
import subprocess
from pathlib import Path

def generate_scram_hash(password, salt=None, iterations=4096):
    if salt is None:
        salt = os.urandom(16)
    else:
        if isinstance(salt, str):
            salt = base64.b64decode(salt)
    
    salted_password = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted_password, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted_password, b"Server Key", hashlib.sha256).digest()
    
    auth_str = f"SCRAM-SHA-256${iterations}:{base64.b64encode(salt).decode()}${base64.b64encode(stored_key).decode()}:{base64.b64encode(server_key).decode()}"
    return auth_str

def main():
    db_user = os.environ.get("DB_USER", "swarmcloud")
    db_pass = os.environ.get("DB_PASS", "swarmcloud")
    db_name = os.environ.get("DB_NAME", "swarmcloud")
    db_host = os.environ.get("BACKEND_DB_HOST", "postgres")
    db_port = os.environ.get("BACKEND_DB_PORT", "5432")

    config_dir = Path("/app/infrastructure/pgbouncer")
    
    # Generate userlist.txt
    print(f"Generating userlist.txt for user: {db_user}")
    scram_hash = generate_scram_hash(db_pass)
    userlist_content = f'"{db_user}" "{scram_hash}"\n'
    (config_dir / "userlist.txt").write_text(userlist_content)

    # Generate pgbouncer.ini
    print(f"Generating pgbouncer.ini")
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
"""
    (config_dir / "pgbouncer.ini").write_text(ini_content)
    print("PgBouncer configuration generated successfully.")

    # Reload PgBouncer
    print("Reloading PgBouncer...")
    try:
        subprocess.run(["docker", "kill", "-s", "HUP", "pgbouncer"], check=True)
        print("PgBouncer reloaded.")
    except subprocess.CalledProcessError as e:
        print(f"Failed to reload PgBouncer: {e}")

if __name__ == "__main__":
    main()
