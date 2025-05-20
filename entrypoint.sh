#!/bin/bash

echo "Waiting for postgres..."

while ! nc -z $DB_HOST $DB_PORT; do
  sleep 0.1
done

echo "PostgreSQL started"

# Apply database migrations
echo "Applying database migrations..."
python manage.py collectstatic --no-input
python manage.py makemigrations
python manage.py migrate

# Start server
echo "Starting server..."
exec python manage.py runserver 0.0.0.0:8000