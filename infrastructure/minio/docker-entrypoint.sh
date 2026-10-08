#!/bin/sh
# Same contract as the former official image: if the command does not start with
# "minio", prepend it, so `command: server /data ...` in the compose file works.
if [ "${1}" != "minio" ] && [ -n "${1}" ]; then
    set -- minio "$@"
fi
exec "$@"
