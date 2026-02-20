#!/bin/bash
set -euo pipefail

umask 077

# Check if .secrets exists and is not writable (e.g. created by docker as root)
if [ -d ".secrets" ] && [ ! -w ".secrets" ]; then
    echo "Error: .secrets directory exists but is not writable."
    echo "This usually happens if 'docker compose up' was run before this script."
    echo "Please run: sudo rm -rf .secrets"
    exit 1
fi

# Security: never commit private keys. This script generates all TLS material
# into a git-ignored directory under .secrets/.
CA_DIR=".secrets/certs/ca"
CERT_DIR=".secrets/certs/internal"
NGINX_CERT_DIR=".secrets/certs/nginx"
SERIAL_FILE="$CA_DIR/ca.srl"

mkdir -p "$CA_DIR" "$CERT_DIR" "$NGINX_CERT_DIR"
chmod 700 "$CA_DIR" "$CERT_DIR"

ensure_ca_passphrase() {
    if [ -z "${CA_PASSPHRASE:-}" ]; then
        read -rsp "Enter passphrase for the internal CA key: " CA_PASSPHRASE
        echo
        if [ -z "$CA_PASSPHRASE" ]; then
            echo "Passphrase cannot be empty" >&2
            exit 1
        fi
    fi
}

# Root CA
ensure_ca_passphrase
trap 'unset CA_PASSPHRASE' EXIT
openssl genrsa -aes256 -passout pass:"$CA_PASSPHRASE" -out "$CA_DIR/ca.key" 4096
openssl req -x509 -new -key "$CA_DIR/ca.key" -passin pass:"$CA_PASSPHRASE" \
    -sha256 -days 3650 -out "$CA_DIR/ca.crt" -subj "/CN=InternalCA"

# Distribute CA certificate (public) alongside issued certs for convenience
install -m 644 "$CA_DIR/ca.crt" "$CERT_DIR/ca.crt"

generate_cert() {
    local name=$1
    local dns=$2
    echo "Generating cert for $name ($dns)"

    openssl genrsa -out "$CERT_DIR/$name.key" 4096
    openssl req -new -key "$CERT_DIR/$name.key" -out "$CERT_DIR/$name.csr" -subj "/CN=$dns"
    
    cat <<EOT > "$CERT_DIR/$name.ext"
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, nonRepudiation, keyEncipherment, dataEncipherment
subjectAltName = @alt_names

[alt_names]
DNS.1 = $dns
DNS.2 = localhost
IP.1 = 127.0.0.1
EOT

    openssl x509 -req -in "$CERT_DIR/$name.csr" -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" \
        -passin pass:"$CA_PASSPHRASE" -CAcreateserial -CAserial "$SERIAL_FILE" \
        -out "$CERT_DIR/$name.crt" -days 365 -sha256 \
        -extfile "$CERT_DIR/$name.ext"
}

generate_cert "minio" "minio"
generate_cert "redis" "redis"
generate_cert "postgres" "postgres"
generate_cert "pgbouncer" "pgbouncer"
generate_cert "webapp" "localhost"

# Redis needs a combined cert/key sometimes, or specific permissions
chmod 644 "$CERT_DIR"/*.crt
chmod 600 "$CERT_DIR"/*.key

# Copy webapp certs for Nginx with restrictive permissions on private key
install -m 644 "$CERT_DIR/webapp.crt" "$NGINX_CERT_DIR/selfsigned.crt"
install -m 600 "$CERT_DIR/webapp.key" "$NGINX_CERT_DIR/selfsigned.key"

# Minio expects certs in a specific structure if mounted to /root/.minio/certs
mkdir -p "$CERT_DIR/minio_certs/CAs"
install -m 644 "$CERT_DIR/minio.crt" "$CERT_DIR/minio_certs/public.crt"
install -m 600 "$CERT_DIR/minio.key" "$CERT_DIR/minio_certs/private.key"
install -m 644 "$CA_DIR/ca.crt" "$CERT_DIR/minio_certs/CAs/ca.crt"

echo "Internal certificates generated in $CERT_DIR"
echo "Public CA certificate available at $CA_DIR/ca.crt (private key locked in $CA_DIR)"
echo "Nginx certificates generated in $NGINX_CERT_DIR"

fix_permissions() {
    echo "Fixing permissions for Postgres and Redis keys..."
    # We use a temporary alpine container to set permissions for UID 999 (postgres)
    # This avoids asking the user for sudo password on the host.
    
    # Get absolute path to .secrets
    SECRETS_DIR="$(pwd)/.secrets"
    
    if ! command -v docker &> /dev/null; then
        echo "Warning: Docker is not found. Please manually run:"
        echo "  sudo chown 999:999 .secrets/certs/internal/postgres.key"
        echo "  sudo chmod 600 .secrets/certs/internal/postgres.key"
        echo "  chmod 644 .secrets/certs/internal/redis.key"
        return
    fi

    echo "Using Docker to set permissions..."
    # We mount the parent directory of .secrets if possible, or just .secrets
    # ensure docker can mount the path.
    docker run --rm -v "$SECRETS_DIR:/secrets" alpine sh -c '
        if [ -f /secrets/certs/internal/postgres.key ]; then
            chown 999:999 /secrets/certs/internal/postgres.key
            chmod 600 /secrets/certs/internal/postgres.key
            echo "Fixed postgres.key permissions"
        fi
        if [ -f /secrets/certs/internal/redis.key ]; then
            chmod 644 /secrets/certs/internal/redis.key
            echo "Fixed redis.key permissions"
        fi
    ' || echo "Failed to fix permissions via Docker. Please check troubleshooting guide."
}

fix_permissions
