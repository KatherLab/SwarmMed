#!/bin/bash
set -e

# Start the password update script in the background
# We assume this script is mounted at /usr/local/bin/update_password.sh
/usr/local/bin/update_password.sh &

# Run the original entrypoint
exec /usr/local/bin/docker-entrypoint.sh "$@"
