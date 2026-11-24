"""Training containers ask only their own Hub for their data manifest.

The request carries the site's project secret. After a server switch the FL
server's host is another site's, whose Hub holds neither the project nor the
data.
"""

import os
import socket
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import requests
from django.test import SimpleTestCase

from . import flare_adapter
from .flare_adapter import _get_manifest_discovery_targets

# What the data adapter reads to find its Hub.
DISCOVERY_VARIABLES = (
    "SWARMMEDHUB_MANIFEST_URLS",
    "SWARMMEDHUB_MANIFEST_URL",
    "SWARMMEDHUB_SERVER_HOST",
    "SWARMMEDHUB_RUNTIME_NETWORK",
    "DOCKER_HOST_IP",
)

# A client container on the host's network, as a Hub of version 1.0.5 starts it
# after a switch to the standby server at 192.0.2.10.
SWITCHED_CLIENT = {
    "SWARMMEDHUB_SERVER_HOST": "192.0.2.10",
    "SWARMMEDHUB_MANIFEST_URL": "https://192.0.2.10:5085",
    "DOCKER_HOST_IP": "198.51.100.1",
}


class ManifestDiscoveryTests(SimpleTestCase):
    def _environment(self, **variables):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in DISCOVERY_VARIABLES:
            os.environ.pop(name, None)
        os.environ.update(variables)

    def test_manifest_discovery_never_asks_the_fl_server_host(self):
        self._environment(**SWITCHED_CLIENT)

        targets = _get_manifest_discovery_targets()

        self.assertEqual(targets[0], "https://localhost:5085")
        self.assertNotIn("192.0.2.10", [urlparse(url).hostname for url in targets])

    def test_manifest_discovery_tries_loopback_before_the_configured_host_address(
        self,
    ):
        # The Docker gateway reaches the Hub only through a relaxed retry, and
        # Django refuses its address as the request's host.
        self._environment(DOCKER_HOST_IP="198.51.100.1")

        targets = _get_manifest_discovery_targets()

        gateway = targets.index("https://198.51.100.1:5085")
        self.assertLess(targets.index("https://localhost:5085"), gateway)
        self.assertLess(targets.index("https://127.0.0.1:5085"), gateway)

    def test_manifest_discovery_on_the_host_network_uses_no_plain_http(self):
        # On the host's network, the names swarmmedhub and app would be looked up
        # in the site's DNS; on a Docker network they are the Hub's containers.
        self._environment(SWARMMEDHUB_RUNTIME_NETWORK="host")
        host_targets = _get_manifest_discovery_targets()
        os.environ["SWARMMEDHUB_RUNTIME_NETWORK"] = "bridge"
        bridge_targets = _get_manifest_discovery_targets()

        self.assertEqual([url for url in host_targets if url.startswith("http:")], [])
        self.assertIn("http://swarmmedhub:8000", bridge_targets)
        self.assertIn("http://app:8000", bridge_targets)

    def test_manifest_secret_never_reaches_the_fl_server_host(self):
        self._environment(
            MANIFEST_SECRET="the-sites-manifest-secret",
            SWARMMEDHUB_PROJECT_ID="9b2e4f0a-0000-4000-8000-000000000001",
            **SWITCHED_CLIENT,
        )
        requested = []

        def fake_get(url, headers=None, **_kwargs):
            requested.append((urlparse(url).hostname, headers))
            if urlparse(url).hostname != "localhost":
                raise requests.ConnectionError(f"No route to host: {url}")
            response = MagicMock(status_code=200)
            response.json.return_value = {
                "exam-001/t1.nii.gz": "https://minio:9000/swarm/p/exam-001/t1.nii.gz"
            }
            return response

        with (
            patch.object(flare_adapter.requests, "get", side_effect=fake_get),
            patch.object(
                flare_adapter.socket, "gethostbyname", side_effect=socket.gaierror
            ),
        ):
            adapter = flare_adapter.FlareDataFileSystem("project-1")
        self.addCleanup(adapter.cleanup)

        self.assertEqual(list(adapter.manifest), ["exam-001/t1.nii.gz"])
        self.assertEqual(
            requested[0],
            ("localhost", {"X-Manifest-Secret": "the-sites-manifest-secret"}),
        )
        self.assertNotIn("192.0.2.10", [host for host, _headers in requested])
