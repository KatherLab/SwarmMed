#!/bin/bash

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