#!/bin/sh
# scripts/tailscale_watcher.sh
# Old name of the overlay helper, kept so that existing commands, cron entries
# and systemd units keep working. It runs scripts/overlay_watcher.py, which
# reads NetBird and Tailscale and writes tmp/overlay_status.json. --provider
# tailscale only puts Tailscale at the top of that file: the Hub's default mode
# (auto) still uses NetBird's address while NetBird is connected. Replace such
# entries with `make overlay`.
exec python3 "$(dirname "$0")/overlay_watcher.py" --provider tailscale "$@"
