#!/bin/bash
# scripts/tailscale_watcher.sh
# This script runs on the host and provides a bridge for the container.

OUTPUT_FILE="./tmp/tailscale_status.json"
mkdir -p ./tmp

echo "Starting Tailscale watcher..."

while true; do
  # Get IPv4 and connection status
  IP=$(tailscale ip -4 2>/dev/null || echo "")
  STATUS=$(tailscale status --json 2>/dev/null | jq -r '.BackendState' 2>/dev/null || echo "Unknown")
  
  # Create a simple JSON state
  echo "{\"ipv4\": \"$IP\", \"backend_state\": \"$STATUS\", \"connected\": \"$( [[ "$STATUS" == "Running" ]] && echo "true" || echo "false" )\"}" > "$OUTPUT_FILE.tmp"
  mv "$OUTPUT_FILE.tmp" "$OUTPUT_FILE"
  
  sleep 30
done
