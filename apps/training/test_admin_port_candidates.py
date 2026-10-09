"""Tests for FLARE admin port probing and the data adapter's fallback hosts."""

import socket
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import requests
from django.test import SimpleTestCase

from . import flare_adapter, runtime, views


class FlareAdminPortCandidateTests(SimpleTestCase):
    """The stock FLARE ports are probed only when no admin port is configured."""

    def _candidates(self, env_port="", default_port=0, build=None):
        build = build or runtime._build_flare_port_candidates
        with patch.dict("os.environ", {"SWARMMEDHUB_FLARE_ADMIN_PORT": env_port}):
            return build(default_port)

    def test_kit_port_is_probed_without_default_ports(self):
        self.assertEqual(self._candidates(default_port=8013), [8013])

    def test_env_port_is_probed_without_default_ports(self):
        self.assertEqual(self._candidates(env_port="8013"), [8013])

    def test_env_port_precedes_a_different_kit_port(self):
        self.assertEqual(
            self._candidates(env_port="8013", default_port=8023), [8013, 8023]
        )

    def test_default_ports_are_probed_when_no_port_is_configured(self):
        for env_port in ("", "not-a-port", "0"):
            with self.subTest(env_port=env_port):
                self.assertEqual(self._candidates(env_port=env_port), [8003, 8002])

    def test_views_share_the_runtime_port_candidates(self):
        self.assertEqual(
            self._candidates(
                default_port=8013, build=views._build_flare_port_candidates
            ),
            [8013],
        )

    def test_pre_submit_probe_dials_only_the_kit_port(self):
        session = MagicMock()
        session.api.host = "server"
        session.api.port = 8013
        session.api.cell = None
        log = MagicMock()
        with (
            patch.dict("os.environ", {"SWARMMEDHUB_FLARE_ADMIN_PORT": ""}),
            patch("nvflare.fuel.flare_api.flare_api.Session", return_value=session),
            patch.object(
                runtime.socket, "create_connection", side_effect=OSError("refused")
            ) as dial,
        ):
            runtime.log_flare_pre_submit_diagnostics(
                log, "admin@nvidia.com", "/nonexistent-startup-kit", "server"
            )

        log.training.warning.assert_not_called()
        self.assertEqual({c.args[0][1] for c in dial.call_args_list}, {8013})


class FlareAdapterFallbackHostTests(SimpleTestCase):
    """A failed presigned URL is retried only on local or configured hosts."""

    PATH = "exam-001/t1.nii.gz"
    URL = "https://minio:9000/swarm/p/data/exam-001/t1.nii.gz?X-Amz-Signature=abc"

    def _adapter(self):
        def load_manifest(adapter):
            return adapter._process_manifest_urls({self.PATH: self.URL})

        with (
            patch.dict("os.environ", {"DOCKER_HOST_IP": "10.0.0.5"}),
            patch.object(
                flare_adapter.socket, "gethostbyname", side_effect=socket.gaierror
            ),
            patch.object(
                flare_adapter.FlareDataFileSystem, "_load_manifest", load_manifest
            ),
        ):
            adapter = flare_adapter.FlareDataFileSystem("project-1")
        self.addCleanup(adapter.cleanup)
        return adapter

    def test_fallback_candidates_omit_hard_coded_overlay_address(self):
        candidates = self._adapter()._manifest_candidates[self.PATH]
        hosts = [urlparse(url).hostname for url in candidates]

        self.assertEqual(candidates[0], self.URL)
        self.assertIn("10.0.0.5", hosts)
        self.assertNotIn("100.127.11.1", hosts)

    def test_failed_url_is_not_retried_on_hard_coded_overlay_address(self):
        adapter = self._adapter()
        with (
            patch.object(
                flare_adapter.requests,
                "get",
                side_effect=requests.ConnectionError("unreachable"),
            ) as probe,
            self.assertRaises(FileNotFoundError),
        ):
            adapter.open(self.PATH)

        probed_hosts = {urlparse(c.args[0]).hostname for c in probe.call_args_list}
        self.assertIn("minio", probed_hosts)
        self.assertNotIn("100.127.11.1", probed_hosts)
