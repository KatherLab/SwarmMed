"""A Hub whose own FL server stopped, or that uses a server another site started."""

from __future__ import annotations

import io
import json
import os
import subprocess  # nosec B404
from contextlib import redirect_stdout
from unittest.mock import patch

from django.test import TestCase
from swarmed_cli.main import main

from .failover import write_active_server
from .runtime import get_local_participant_status
from .test_failover import NetworkFixture
from .test_worker_permissions import RuntimeFixture


class StaleServerLogTests(NetworkFixture, TestCase):
    """Client rows come from the server's log only while its container runs."""

    # The log of this Hub's FL server, as it was when the server stopped.
    SERVER_LOG = (
        "2026-11-23 15:30:00,000 - ClientManager - INFO - Client: New client "
        "node-A@192.0.2.20 joined. Sent token: 6f1c.  Total clients: 1\n"
    )

    def _statuses(self, *, server_running):
        def fake_run(command, **_kwargs):
            stdout = self.SERVER_LOG if command[:2] == ["docker", "logs"] else ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with (
            patch("network.runtime._safe_cache_get", return_value=None),
            patch("network.runtime._safe_cache_set"),
            patch("network.runtime.get_local_addresses", return_value=frozenset()),
            patch(
                "network.runtime._is_container_running", return_value=server_running
            ),
            patch("network.runtime.subprocess.run", side_effect=fake_run),
        ):
            return get_local_participant_status(self.network)

    def test_clients_of_a_stopped_server_are_not_joined(self):
        # As at a coordinating Hub that comes back while the standby serves.
        self.assertEqual(
            self._statuses(server_running=False),
            {"server": "Offline", "node-A": "Offline", "node-B": "Offline"},
        )

    def test_clients_of_a_running_server_are_joined(self):
        self.assertEqual(
            self._statuses(server_running=True),
            {"server": "Online", "node-A": "Joined", "node-B": "Offline"},
        )


class StartRefusalCLITests(RuntimeFixture, TestCase):
    """'swarmed network start' at a Hub switched to a server another site started."""

    def test_start_error_names_the_switch(self):
        # A server-only coordinator that switched to the standby at 192.0.2.10.
        write_active_server(self.network, "192.0.2.10", started_server=False)
        stdout = io.StringIO()

        with (
            patch.dict(
                os.environ,
                {
                    "SWARMMEDHUB_RUN_TASKS_INLINE": "1",
                    "SWARMMEDHUB_SERVER_ONLY_MODE": "true",
                },
            ),
            # Keep the checkout's .env, which the CLI loads, out of the test.
            patch("swarmed_cli.bootstrap.load_dotenv"),
            # A wait that does not return polls again.
            patch("swarmed_cli.support.time.sleep", side_effect=AssertionError),
            self._docker() as runs,
            redirect_stdout(stdout),
        ):
            os.environ.pop("SWARMMEDHUB_CLIENT_ONLY_MODE", None)
            code = main(
                [
                    "network",
                    "start",
                    str(self.network.identifier),
                    "--wait",
                    "--user",
                    "operator",
                    "--json",
                ]
            )

        output = json.loads(stdout.getvalue())
        self.assertEqual(code, 2, output)
        self.assertEqual(runs, [])
        self.network.refresh_from_db()
        self.assertEqual(self.network.status, "ERROR")
        self.assertIn(
            "This Hub is switched to the FL server at 192.0.2.10", output["errors"][0]
        )
