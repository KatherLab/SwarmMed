#!/bin/bash

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
  python manage.py collectstatic --no-input || echo "Collectstatic failed, continuing..."
  python manage.py migrate
else
  echo "Skipping migrations and collectstatic as requested..."
fi

# Start server
echo "Starting server..."
exec "$@"