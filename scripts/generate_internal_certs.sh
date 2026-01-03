#!/bin/bash
set -e

CERT_DIR="infrastructure/certs/internal"
mkdir -p $CERT_DIR

# Root CA
openssl genrsa -out $CERT_DIR/ca.key 4096
openssl req -x509 -new -nodes -key $CERT_DIR/ca.key -sha256 -days 3650 -out $CERT_DIR/ca.crt -subj "/CN=InternalCA"

generate_cert() {
    local name=$1
    local dns=$2
    echo "Generating cert for $name ($dns)"
    
    openssl genrsa -out $CERT_DIR/$name.key 2048
    openssl req -new -key $CERT_DIR/$name.key -out $CERT_DIR/$name.csr -subj "/CN=$dns"
    
    cat <<EOT > $CERT_DIR/$name.ext
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, nonRepudiation, keyEncipherment, dataEncipherment
subjectAltName = @alt_names

[alt_names]
DNS.1 = $dns
DNS.2 = localhost
IP.1 = 127.0.0.1
EOT

    openssl x509 -req -in $CERT_DIR/$name.csr -CA $CERT_DIR/ca.crt -CAkey $CERT_DIR/ca.key         -CAcreateserial -out $CERT_DIR/$name.crt -days 365 -sha256 -extfile $CERT_DIR/$name.ext
}

generate_cert "minio" "minio"
generate_cert "redis" "redis"
generate_cert "postgres" "postgres"
generate_cert "pgbouncer" "pgbouncer"
generate_cert "webapp" "localhost"

# Redis needs a combined cert/key sometimes, or specific permissions
chmod 644 $CERT_DIR/*.crt
chmod 600 $CERT_DIR/*.key

# Copy webapp certs to the main certs directory for Nginx
cp $CERT_DIR/webapp.crt infrastructure/certs/selfsigned.crt
cp $CERT_DIR/webapp.key infrastructure/certs/selfsigned.key

# Minio expects certs in a specific structure if mounted to /root/.minio/certs
mkdir -p $CERT_DIR/minio_certs/CAs
cp $CERT_DIR/minio.crt $CERT_DIR/minio_certs/public.crt
cp $CERT_DIR/minio.key $CERT_DIR/minio_certs/private.key
cp $CERT_DIR/ca.crt $CERT_DIR/minio_certs/CAs/ca.crt

echo "Internal certificates generated in $CERT_DIR"
