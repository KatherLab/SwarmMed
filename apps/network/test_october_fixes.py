"""Regression tests for the startup-package import form, the server host
override and the participant status patterns."""

import inspect
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, sentinel

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase

from . import views as network_views
from .runtime import get_local_participant_status
from .tasks import _configured_server_host


class NewNetworkImportTests(SimpleTestCase):
    """The web form's import option must reach the startup-kit import."""

    def _post_new_network(self, creation_method):
        request = RequestFactory().post(
            "/network/new/",
            {
                "creation_method": creation_method,
                "title": "Imported Network",
                "description": "",
                "startup_package": SimpleUploadedFile(
                    "kit.zip", b"PK\x05\x06" + bytes(18), "application/zip"
                ),
            },
        )
        request.user = SimpleNamespace(is_authenticated=True)
        # Bypass the login and project decorators, which need the database.
        view = inspect.unwrap(network_views.new_network)
        with patch.object(network_views, "UserCurrentProject") as current, patch.object(
            network_views.network_services, "import_network"
        ) as import_network:
            current.objects.get.return_value.project = sentinel.project
            response = view(request)

        self.assertEqual(response.status_code, 302)
        return import_network

    def test_form_import_option_imports_the_package(self):
        template = Path(
            settings.BASE_DIR, "templates", "apps", "network", "new_network.html"
        ).read_text()
        value = re.search(
            r'id="upload-package"[^>]*\bvalue="([^"]*)"', template
        ).group(1)

        import_network = self._post_new_network(value)

        import_network.assert_called_once()
        kwargs = import_network.call_args.kwargs
        self.assertIs(kwargs["project"], sentinel.project)
        self.assertEqual(kwargs["name"], "Imported Network")
        self.assertEqual(kwargs["package_source"].name, "kit.zip")

    def test_import_and_legacy_upload_values_both_import(self):
        for value in ("import", "upload"):
            with self.subTest(creation_method=value):
                self._post_new_network(value).assert_called_once()


class ConfiguredServerHostTests(SimpleTestCase):
    """SWARMMEDHUB_SERVER_HOST overrides the kit's server_host.txt."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.startup_dir = temp_dir.name

    def _server_host(self, env_host=None):
        with patch.dict(os.environ):
            os.environ.pop("SWARMMEDHUB_SERVER_HOST", None)
            if env_host is not None:
                os.environ["SWARMMEDHUB_SERVER_HOST"] = env_host
            return _configured_server_host(self.startup_dir)

    def test_environment_applies_when_kit_has_no_server_host_file(self):
        self.assertEqual(self._server_host(" 100.64.0.7 "), "100.64.0.7")

    def test_environment_wins_over_server_host_file(self):
        Path(self.startup_dir, "server_host.txt").write_text("100.64.0.1\n")

        self.assertEqual(self._server_host("100.64.0.7"), "100.64.0.7")

    def test_server_host_file_used_without_environment(self):
        Path(self.startup_dir, "server_host.txt").write_text("100.64.0.1\n")

        self.assertEqual(self._server_host(), "100.64.0.1")
        self.assertEqual(self._server_host("  "), "100.64.0.1")


class _Participants(list):
    """Just enough of a participant queryset for the status parser."""

    def filter(self, role):
        return _Participants(p for p in self if p.role == role)

    def count(self):
        return len(self)


class ParticipantStatusPatternTests(SimpleTestCase):
    """NVFlare's capitalised log lines must count although the logs are
    lower-cased before matching."""

    def _statuses(self, server_log_lines, client_names):
        participants = _Participants(
            SimpleNamespace(participant_id=name, role="CLIENT", ip="100.64.0.9")
            for name in client_names
        )
        network = SimpleNamespace(
            identifier="0123456789abcdef",
            status="RUNNING",
            participants=SimpleNamespace(all=lambda: participants),
        )
        server_logs = "".join(
            f"2026-10-08 10:00:0{i},000 - ClientManager - INFO - {line}\n"
            for i, line in enumerate(server_log_lines)
        )

        def fake_run(command, **kwargs):
            stdout = server_logs if command[:2] == ["docker", "logs"] else ""
            return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

        with patch("network.runtime._safe_cache_get", return_value=None), patch(
            "network.runtime._safe_cache_set"
        ), patch(
            "network.runtime.get_tailscale_ip", return_value="100.64.0.1"
        ), patch(
            "network.runtime._is_container_running", return_value=False
        ), patch("network.runtime.subprocess.run", side_effect=fake_run):
            return get_local_participant_status(network)

    def test_login_line_marks_client_joined(self):
        statuses = self._statuses(
            [
                "Client: New client site-1@172.18.0.3 joined. "
                "Sent token: 6f1c.  Total clients: 1"
            ],
            ["site-1"],
        )

        self.assertEqual(statuses, {"site-1": "Joined"})

    def test_reactivation_line_marks_client_joined(self):
        statuses = self._statuses(
            ["Re-activate the client: site-1 at site-1 with token: 6f1c"],
            ["site-1"],
        )

        self.assertEqual(statuses, {"site-1": "Joined"})

    def test_left_line_marks_only_that_client_disconnected(self):
        statuses = self._statuses(
            [
                "Client: New client site-1@172.18.0.3 joined. "
                "Sent token: 6f1c.  Total clients: 1",
                "Client: New client site-2@172.18.0.4 joined. "
                "Sent token: 9a2d.  Total clients: 2",
                "Client Name:site-2 \tToken: 9a2d left.  Total clients: 1",
            ],
            ["site-1", "site-2"],
        )

        self.assertEqual(statuses, {"site-1": "Joined", "site-2": "Disconnected"})
