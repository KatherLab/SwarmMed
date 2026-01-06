#!/bin/bash
set -e

wait_for_docker() {
  if [ -z "${DOCKER_HOST:-}" ]; then
    return
  fi

  if [ -n "${DOCKER_CERT_PATH:-}" ]; then
    for cert_file in ca.pem cert.pem key.pem; do
      until [ -f "$DOCKER_CERT_PATH/$cert_file" ]; do
        echo "Waiting for Docker client certificate $cert_file..."
        sleep 1
      done
    done
  fi

  echo "Waiting for sandbox Docker daemon..."
  local attempt=0
  until DOCKER_HOST="$DOCKER_HOST" DOCKER_TLS_VERIFY="${DOCKER_TLS_VERIFY:-}" DOCKER_CERT_PATH="${DOCKER_CERT_PATH:-}" docker info >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "Sandbox Docker daemon is unavailable" >&2
      exit 1
    fi
    sleep 2
  done
}

# Generate PgBouncer configuration from environment variables
if [ -f "/app/scripts/setup_pgbouncer.py" ]; then
  echo "Syncing PgBouncer configuration..."
  python3 /app/scripts/setup_pgbouncer.py
fi

# Fix permissions for volume-mounted directories
echo "Fixing permissions..."
chown -R appuser:appuser /app/tmp /app/media /app/workspaces /app/staticfiles || echo "Warning: Failed to fix some permissions"

echo "Waiting for postgres..."
while ! nc -z $DB_HOST $DB_PORT; do
  sleep 0.1
done
echo "PostgreSQL started"

echo "Waiting for redis..."
while ! nc -z redis 6379; do
  sleep 0.1
done
echo "Redis started"

echo "Waiting for minio..."
while ! nc -z minio 9000; do
  sleep 0.1
done
echo "Minio started"

wait_for_docker

# Apply database migrations
if [ -z "$SKIP_MIGRATIONS" ]; then
  echo "Applying database migrations and collecting static files..."
  gosu appuser python manage.py collectstatic --no-input || echo "Collectstatic failed, continuing..."
  gosu appuser python manage.py migrate
else
  echo "Skipping migrations and collectstatic as requested..."
fi

# Start server
echo "Starting server..."
exec gosu appuser "$@"