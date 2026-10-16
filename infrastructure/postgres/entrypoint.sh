#!/bin/bash
set -e

# Copy SSL key to container-native filesystem with strict 0600 postgres:postgres
# ownership so PostgreSQL SSL starts cleanly on macOS/Windows Docker Desktop
# (VirtioFS/NTFS bind mounts) as well as native Linux filesystems.
if [ -f /var/lib/postgresql/server.key ]; then
    cp /var/lib/postgresql/server.key /tmp/postgres_server.key
    chown postgres:postgres /tmp/postgres_server.key 2>/dev/null || true
    chmod 600 /tmp/postgres_server.key
    set -- "$@" -c ssl_key_file=/tmp/postgres_server.key
fi

# Start the password update script in the background
# We assume this script is mounted at /usr/local/bin/update_password.sh
/usr/local/bin/update_password.sh &

# Run the original entrypoint
exec /usr/local/bin/docker-entrypoint.sh "$@"
