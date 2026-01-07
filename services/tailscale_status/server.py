#!/usr/bin/env python3
"""Minimal HTTP bridge exposing limited Tailscale status information."""

import json
import os
import secrets
import shutil
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TAILSCALE_BIN = os.environ.get("TAILSCALE_BIN") or shutil.which("tailscale") or "/usr/bin/tailscale"
TAILSCALE_SOCKET = os.environ.get("TAILSCALE_SOCKET", "/var/run/tailscale/tailscaled.sock")
PORT = int(os.environ.get("PORT", "9002"))
HOST = os.environ.get("TAILSCALE_STATUS_BIND", "0.0.0.0")
STATUS_TOKEN = os.environ.get("TAILSCALE_STATUS_TOKEN")
EXPOSE_RAW_STATUS = os.environ.get("TAILSCALE_EXPOSE_RAW", "False").lower() in {"1", "true", "yes"}


def collect_status():
    env = os.environ.copy()
    env["TAILSCALE_SOCKET"] = TAILSCALE_SOCKET
    try:
        proc = subprocess.run(
            [TAILSCALE_BIN, "status", "--json"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout)
        ips = data.get("Self", {}).get("TailscaleIPs", [])
        ipv4 = next((ip for ip in ips if ":" not in ip), None)
        connected = data.get("BackendState") in {"Running", "NeedsLogin", "Starting"}
        payload = {
            "connected": connected,
            "ipv4": ipv4,
        }
        if EXPOSE_RAW_STATUS:
            payload["raw"] = data
        return payload
    except Exception as exc:  # pragma: no cover - defensive path
        print(f"Error collecting Tailscale status: {exc}")
        if hasattr(exc, 'stderr') and exc.stderr:
            print(f"Tailscale stderr: {exc.stderr}")
        return {"error": str(exc), "connected": False}


class StatusHandler(BaseHTTPRequestHandler):
    def _is_authorized(self):
        if not STATUS_TOKEN:
            return True

        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            if secrets.compare_digest(token, STATUS_TOKEN):
                return True

        header_token = self.headers.get("X-Auth-Token")
        if header_token and secrets.compare_digest(header_token.strip(), STATUS_TOKEN):
            return True

        return False

    def _require_auth(self):
        self._send_json(
            {"error": "Unauthorized"},
            status_code=HTTPStatus.UNAUTHORIZED,
        )

    def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        print(f"Received GET request for {self.path} from {self.client_address[0]}")
        if not self._is_authorized():
            self._require_auth()
            return

        if self.path in {"/health", "/healthz"}:
            self._send_json({"status": "ok"})
            return

        if self.path.startswith("/status"):
            payload = collect_status()
            status = HTTPStatus.OK if "error" not in payload else HTTPStatus.SERVICE_UNAVAILABLE
            self._send_json(payload, status_code=status)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format, *args):  # noqa: A003 - signature locked by BaseHTTPRequestHandler
        return  # Silence default logging noise

    def _send_json(self, payload, status_code=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    print(f"Starting Tailscale status server on {HOST}:{PORT}...")
    print(f"Using Tailscale binary: {TAILSCALE_BIN}")
    print(f"Using Tailscale socket: {TAILSCALE_SOCKET}")
    server = ThreadingHTTPServer((HOST, PORT), StatusHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        print("Shutting down Tailscale status server.")
        server.server_close()


if __name__ == "__main__":
    main()
