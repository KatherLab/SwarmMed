#!/bin/bash
set -e

# Path to pg_hba.conf
PG_HBA_CONF="$PGDATA/pg_hba.conf"

echo "Enforcing SSL in $PG_HBA_CONF..."

# Replace host with hostssl for all non-local connections
# This covers IPv4, IPv6, and all databases/users
# We use a temp file to avoid issues with sed in-place on some systems/mounts
sed 's/^host[[:space:]]\+/hostssl /' "$PG_HBA_CONF" > "$PG_HBA_CONF.tmp" && mv "$PG_HBA_CONF.tmp" "$PG_HBA_CONF"

# Also ensure local connections (if used) are restricted if desired, 
# but hostssl only applies to TCP/IP.
# The previous line handles 'host all all all scram-sha-256' -> 'hostssl all all all scram-sha-256'

echo "SSL enforcement complete."
