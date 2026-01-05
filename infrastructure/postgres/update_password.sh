#!/bin/bash
set -e

echo "Waiting for PostgreSQL to start..."
# Loop until we can connect to postgres locally
# We use 127.0.0.1 to force TCP connection which is trusted in pg_hba.conf for localhost
# Using local socket might also work depending on config, but TCP 127.0.0.1 is explicitly trusted in the log output I saw earlier.
until pg_isready -h 127.0.0.1 -U "$POSTGRES_USER"; do
  sleep 2
done

echo "PostgreSQL started. Updating password for user $POSTGRES_USER..."

# Update the password
psql -h 127.0.0.1 -U "$POSTGRES_USER" -c "ALTER USER \"$POSTGRES_USER\" WITH PASSWORD '$POSTGRES_PASSWORD';"

echo "Password updated successfully from environment variable."
