"""Regression tests for installation-path defects found by a fresh-install test.

`swarmed network start --wait` reported ``"ok": true`` when the network ended in
ERROR, and `make env` wrote 172.17.0.1 as DOCKER_HOST_IP on every Linux host
(MinIO then cannot bind where Docker's bridge has another address) into a
world-readable .env.
"""

import importlib.util
import json
import os
import stat
import subprocess  # nosec B404
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from swarmed_cli.main import main

from network.models import SwarmNetwork
from project.models import Project


def _load_setup_env():
    path = Path(settings.BASE_DIR) / "scripts" / "setup_env.py"
    spec = importlib.util.spec_from_file_location("setup_env_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnvFileTests(SimpleTestCase):
    def setUp(self):
        self.setup_env = _load_setup_env()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = os.path.join(self.tmp.name, ".env")

    def _mode(self):
        return stat.S_IMODE(os.stat(self.env_path).st_mode)

    def test_new_env_file_is_readable_only_by_its_owner(self):
        self.setup_env.write_env_file(self.env_path, ["SECRET_KEY=abc", "DEBUG=False"])
        self.assertEqual(self._mode(), 0o600)
        self.assertEqual(
            Path(self.env_path).read_text(), "SECRET_KEY=abc\nDEBUG=False\n"
        )

    def test_existing_world_readable_env_file_is_tightened(self):
        Path(self.env_path).write_text("OLD=1\n")
        os.chmod(self.env_path, 0o664)
        self.setup_env.write_env_file(self.env_path, ["NEW=2"])
        self.assertEqual(self._mode(), 0o600)
        self.assertEqual(Path(self.env_path).read_text(), "NEW=2\n")


class DockerBridgeGatewayTests(SimpleTestCase):
    def setUp(self):
        self.setup_env = _load_setup_env()

    def _detect(self, **run_kwargs):
        with patch.object(self.setup_env.subprocess, "run", **run_kwargs):
            return self.setup_env.detect_docker_bridge_gateway()

    def test_reads_the_gateway_docker_reports(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="172.18.0.1\n"
        )
        self.assertEqual(self._detect(return_value=result), "172.18.0.1")

    def test_falls_back_when_docker_is_missing(self):
        self.assertEqual(
            self._detect(side_effect=FileNotFoundError("docker")), "172.17.0.1"
        )

    def test_falls_back_when_the_output_is_not_an_address(self):
        for stdout in ("", "<no value>\n", "Error: No such network: bridge\n"):
            with self.subTest(stdout=stdout):
                result = subprocess.CompletedProcess(
                    args=[], returncode=1, stdout=stdout
                )
                self.assertEqual(self._detect(return_value=result), "172.17.0.1")


class NetworkStartFailureTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="starter", password="test-password"
        )  # nosec B106
        project = Project.objects.create(title="Start Project", author=self.user)
        self.network = SwarmNetwork.objects.create(
            name="Start Network", project=project, author=self.user, status="STOPPED"
        )

    def _start_with_wait(self, final_status):
        def finish(**_kwargs):
            self.network.status = final_status
            self.network.save(update_fields=["status"])
            return self.network

        stdout = StringIO()
        with (
            patch("network.services.start_network", return_value=self.network),
            patch("swarmed_cli.support.wait_for_state", side_effect=finish),
            redirect_stdout(stdout),
        ):
            code = main(
                [
                    "network",
                    "start",
                    "--user",
                    self.user.username,
                    "--json",
                    "--wait",
                    str(self.network.identifier),
                ]
            )
        return code, json.loads(stdout.getvalue())

    def test_network_that_ends_in_error_is_reported_as_a_failure(self):
        code, payload = self._start_with_wait("ERROR")
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertIn("did not start", payload["errors"][0])

    def test_network_that_runs_is_reported_as_success(self):
        code, payload = self._start_with_wait("RUNNING")
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["status"], "RUNNING")
