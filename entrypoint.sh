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

# Apply database migrations
echo "Applying database migrations..."
python manage.py collectstatic --no-input
python manage.py makemigrations
python manage.py migrate

# Start server
echo "Starting server..."
exec "$@"