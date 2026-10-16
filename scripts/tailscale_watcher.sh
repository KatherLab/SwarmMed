#!/bin/bash
# scripts/tailscale_watcher.sh
# This script runs on the host and provides a bridge for the container.

OUTPUT_FILE="./tmp/tailscale_status.json"
mkdir -p ./tmp

echo "Starting Tailscale watcher..."

resolve_tailscale_bin() {
  if command -v tailscale >/dev/null 2>&1; then
    echo "tailscale"
  elif [ -x "/Applications/Tailscale.app/Contents/MacOS/Tailscale" ]; then
    echo "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
  else
    echo ""
  fi
}

parse_backend_state() {
  if command -v jq >/dev/null 2>&1; then
    jq -r '.BackendState' 2>/dev/null || echo "Unknown"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json, sys; print(json.load(sys.stdin).get("BackendState", "Unknown"))' 2>/dev/null || echo "Unknown"
  else
    echo "Unknown"
  fi
}

while true; do
  TS_BIN=$(resolve_tailscale_bin)
  if [ -n "$TS_BIN" ]; then
    IP=$("$TS_BIN" ip -4 2>/dev/null | head -n 1 || echo "")
    STATUS=$("$TS_BIN" status --json 2>/dev/null | parse_backend_state)
  else
    IP=""
    STATUS="Unknown"
  fi

  # Create a simple JSON state
  echo "{\"ipv4\": \"$IP\", \"backend_state\": \"$STATUS\", \"connected\": \"$( [[ "$STATUS" == "Running" ]] && echo "true" || echo "false" )\"}" > "$OUTPUT_FILE.tmp"
  mv "$OUTPUT_FILE.tmp" "$OUTPUT_FILE"

  sleep 30
done
