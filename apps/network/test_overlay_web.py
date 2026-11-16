"""The overlay address in the Network page, the new-network form, create_network()
and the CLI's --server-ip. Status files go into a temporary BASE_DIR; startup kits
are not generated. Addresses are documentation examples."""

import json
import os
import tempfile
import time
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from swarmed_cli.main import main as cli_main

from project.models import Project, UserCurrentProject

from . import overlay
from . import services as network_services
from .models import SwarmNetwork, SwarmParticipant, UserCurrentNetwork


def _status_document(
    ip="100.64.0.10", connected=True, state="Connected", seconds_ago=0
):
    entry = {
        "installed": True,
        "ip": ip,
        "connected": connected,
        "state": state,
        "peers": {"connected": 3, "total": 3},
        "error": None,
    }
    updated = datetime.fromtimestamp(time.time() - seconds_ago, tz=UTC)
    return {
        "schema": 1,
        "provider": "netbird",
        **entry,
        "updated_at": updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval_seconds": 30,
        "providers": {
            "netbird": entry,
            "tailscale": {
                "installed": False,
                "ip": None,
                "connected": None,
                "state": None,
                "peers": None,
                "error": None,
            },
        },
    }


TESTDATA = Path(__file__).resolve().parent / "testdata" / "overlay"


def _real_status_text(name, seconds_ago=5):
    """A status file the helper wrote next to NetBird 0.82.1, time stamp moved."""
    payload = json.loads((TESTDATA / name).read_text())
    updated = datetime.fromtimestamp(time.time() - seconds_ago, tz=UTC)
    payload["updated_at"] = updated.strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps(payload)


class OverlayWebTestCase(TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.base = Path(temp_dir.name)
        (self.base / "tmp").mkdir()
        settings_override = override_settings(BASE_DIR=str(self.base))
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        env = patch.dict(os.environ, {"SWARMMEDHUB_HOSTNAME": "hub-host"})
        env.start()
        self.addCleanup(env.stop)
        for key in (
            "SWARMMEDHUB_OVERLAY",
            "SWARMMEDHUB_LOCAL_ADDRESSES",
            "SWARMMEDHUB_OVERLAY_STALE_SECONDS",
        ):
            os.environ.pop(key, None)
        overlay.reset_warnings()
        kits = patch("network.services.generate_flare_startup_kit")
        self.generate_kits = kits.start()
        self.addCleanup(kits.stop)

        self.user = User.objects.create_user(username="alice", password="test-password")  # nosec B106
        self.user.profile.accepted_terms = True
        self.user.profile.accepted_policy = True
        self.user.profile.save(update_fields=["accepted_terms", "accepted_policy"])
        self.project = Project.objects.create(title="Overlay Project", author=self.user)
        UserCurrentProject.objects.create(user=self.user, project=self.project)
        self.client = Client()
        self.client.force_login(self.user)

    def write_status(self, **kwargs):
        path = self.base / "tmp" / "overlay_status.json"
        path.write_text(json.dumps(_status_document(**kwargs)))


class NetworkPageTests(OverlayWebTestCase):
    def _network(self, server_ip="100.64.0.10"):
        network = SwarmNetwork.objects.create(
            name="Study",
            project=self.project,
            author=self.user,
            creation_method="CREATED",
            status="PROVISIONED",
        )
        SwarmParticipant.objects.create(
            network=network,
            user=self.user,
            role="SERVER",
            participant_id="server",
            org="swarm_control_plane",
            ip=server_ip,
        )
        SwarmParticipant.objects.create(
            network=network,
            user=self.user,
            role="CLIENT",
            participant_id="site-a",
            org="org_site_a",
            ip="100.64.0.11",
        )
        UserCurrentNetwork.objects.create(user=self.user, network=network)
        return network

    def _get(self):
        with patch("network.views._get_local_participant_status", return_value={}):
            response = self.client.get(reverse("network:network"))
        self.assertEqual(response.status_code, 200)
        return response

    def test_connected_netbird(self):
        self.write_status()
        self._network()

        with override_settings(ALLOWED_HOSTS=["testserver", "100.64.0.10"]):
            response = self._get()

        status = response.context["overlay"]
        self.assertEqual((status.provider, status.ip), ("netbird", "100.64.0.10"))
        self.assertEqual(response.context["overlay_provider_label"], "NetBird")
        self.assertEqual(response.context["tailscale_ip"], "100.64.0.10")
        self.assertEqual(response.context["tailscale_status"], "connected")
        self.assertFalse(response.context["overlay_ip_not_allowed"])
        self.assertIsNone(response.context["server_address_unrecognised"])
        content = response.content.decode()
        self.assertIn("Network and overlay status", content)
        self.assertIn("Overlay address (NetBird)", content)
        self.assertIn("3 of 3 peers connected", content)
        self.assertIn('id="networkStatus"', content)
        self.assertNotIn("VPN", content)
        self.assertNotIn("vpnStatus", content)

    def test_stale_status_is_shown_as_stale(self):
        self.write_status(seconds_ago=7 * 60)

        response = self._get()

        self.assertEqual(response.context["overlay"].connection, "stale")
        self.assertEqual(response.context["tailscale_status"], "disconnected")
        content = response.content.decode()
        self.assertIn("Stale: no update from the overlay helper for 7 min", content)
        self.assertIn("This is the last known address", content)
        self.assertIn("100.64.0.10", content)

    def test_no_status_file(self):
        response = self._get()

        self.assertEqual(response.context["tailscale_ip"], "Not Available")
        content = response.content.decode()
        self.assertIn("No status from the overlay helper", content)
        self.assertIn("make overlay", content)

    def test_real_status_with_management_server_down(self):
        (self.base / "tmp" / "overlay_status.json").write_text(
            _real_status_text("overlay_status_netbird_0.82.1_management_down.json")
        )

        response = self._get()

        self.assertEqual(response.context["overlay"].connection, "connected")
        content = response.content.decode()
        self.assertIn("192.0.2.13", content)
        self.assertIn("Connected; 1 of 1 peers connected", content)
        self.assertIn(
            "management: rpc error: code = Internal desc = server closed the "
            "stream without sending trailers",
            content,
        )

    def test_real_status_when_logged_out(self):
        (self.base / "tmp" / "overlay_status.json").write_text(
            _real_status_text("overlay_status_netbird_0.82.1_logged_out.json")
        )

        response = self._get()

        self.assertEqual(response.context["tailscale_ip"], "Not Available")
        content = response.content.decode()
        self.assertIn("Not connected (NeedsLogin)", content)
        self.assertNotIn("0 of 0 peers", content)

    def test_address_missing_from_allowed_hosts_is_reported(self):
        self.write_status()
        with override_settings(ALLOWED_HOSTS=["testserver"]):
            response = self._get()
        self.assertTrue(response.context["overlay_ip_not_allowed"])
        self.assertIn('id="overlayAllowedHostsWarning"', response.content.decode())

    def test_server_address_that_is_not_this_hosts(self):
        self.write_status()
        self._network(server_ip="10.90.0.1")

        response = self._get()

        self.assertEqual(response.context["server_address_unrecognised"], "10.90.0.1")
        self.assertIn("SWARMMEDHUB_LOCAL_ADDRESSES", response.content.decode())

        with patch.dict(os.environ, {"SWARMMEDHUB_LOCAL_ADDRESSES": "10.90.0.1"}):
            response = self._get()
        self.assertIsNone(response.context["server_address_unrecognised"])


class NewNetworkFormTests(OverlayWebTestCase):
    def _post(self, **fields):
        data = {
            "creation_method": "create",
            "title": "Hospital Swarm",
            "description": "Two sites",
            "clients": [json.dumps({"name": "site-a", "ip": "100.64.0.11"})],
            **fields,
        }
        return self.client.post(reverse("network:new_network"), data)

    def _server_ip(self, network):
        return network.participants.get(role="SERVER").ip

    def test_form_proposes_the_overlay_address(self):
        self.write_status()

        response = self.client.get(reverse("network:new_network"))

        self.assertEqual(response.context["overlay_address"], "100.64.0.10")
        self.assertFalse(response.context["server_ip_required"])
        content = response.content.decode()
        options = content[content.index('id="create-new-options"') :]
        options = options[: options.index('id="upload-package-options"')]
        self.assertIn('name="server_ip"', options)
        self.assertIn(
            "Leave empty to use this host's NetBird address, 100.64.0.10.", content
        )

    def test_form_names_the_configured_address_without_an_overlay(self):
        os.environ["SWARMMEDHUB_OVERLAY"] = "none"
        os.environ["SWARMMEDHUB_LOCAL_ADDRESSES"] = "127.0.0.1,10.90.0.1"

        response = self.client.get(reverse("network:new_network"))

        self.assertEqual(response.context["overlay_address"], "10.90.0.1")
        content = " ".join(response.content.decode().split())
        self.assertIn(
            "Leave empty to use the address from "
            "<code>SWARMMEDHUB_LOCAL_ADDRESSES</code>, 10.90.0.1.",
            content,
        )
        self.assertNotIn("No overlay address", content)

    def test_form_with_only_a_loopback_configured_address_names_the_reason(self):
        os.environ["SWARMMEDHUB_OVERLAY"] = "none"
        os.environ["SWARMMEDHUB_LOCAL_ADDRESSES"] = "127.0.0.1"

        response = self.client.get(reverse("network:new_network"))

        self.assertTrue(response.context["server_ip_required"])
        content = " ".join(response.content.decode().split())
        self.assertIn(
            "because SWARMMEDHUB_OVERLAY is none and SWARMMEDHUB_LOCAL_ADDRESSES "
            "names no address that other sites can connect to",
            content,
        )

    def test_form_requires_an_address_without_a_current_overlay_address(self):
        response = self.client.get(reverse("network:new_network"))

        self.assertTrue(response.context["server_ip_required"])
        content = response.content.decode()
        field = content[content.index('id="server-ip"') :]
        self.assertIn("required", field[: field.index("/>")])
        self.assertIn("this host's overlay address cannot be used", content)

    def test_form_with_netbird_logged_out_names_the_reason(self):
        (self.base / "tmp" / "overlay_status.json").write_text(
            _real_status_text("overlay_status_netbird_0.82.1_logged_out.json")
        )

        response = self.client.get(reverse("network:new_network"))

        self.assertTrue(response.context["server_ip_required"])
        self.assertIn(
            "because NetBird is not connected (NeedsLogin)",
            response.content.decode(),
        )

    def test_typed_address_is_used(self):
        self.write_status()

        response = self._post(server_ip=" 10.90.0.1 ")

        self.assertEqual(response.status_code, 302)
        network = SwarmNetwork.objects.get(name="Hospital Swarm")
        self.assertEqual(self._server_ip(network), "10.90.0.1")
        clients = dict(
            network.participants.filter(role="CLIENT").values_list(
                "participant_id", "ip"
            )
        )
        self.assertEqual(clients, {"site-a": "100.64.0.11", "hub-host": "10.90.0.1"})
        self.assertEqual(self.generate_kits.call_args.kwargs["server_ip"], "10.90.0.1")

    def test_empty_field_uses_the_overlay_address(self):
        self.write_status()

        response = self._post(server_ip="")

        self.assertEqual(response.status_code, 302)
        network = SwarmNetwork.objects.get(name="Hospital Swarm")
        self.assertEqual(self._server_ip(network), "100.64.0.10")

    def test_invalid_address_shows_the_form_again_and_creates_nothing(self):
        self.write_status()

        response = self._post(server_ip="fl-server.example.org")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(SwarmNetwork.objects.exists())
        self.generate_kits.assert_not_called()
        self.assertIn(
            "cannot be used as the FL server address", response.context["form_error"]
        )
        form = response.context["form"]
        self.assertEqual(form["title"], "Hospital Swarm")
        self.assertEqual(form["server_ip"], "fl-server.example.org")
        self.assertEqual(form["clients"], [{"name": "site-a", "ip": "100.64.0.11"}])
        content = response.content.decode()
        self.assertIn('id="server-ip-error"', content)
        self.assertIn('value="Hospital Swarm"', content)
        self.assertIn('"name": "site-a"', content)  # rebuilt by the page's script

    def test_empty_field_with_stale_overlay_is_refused(self):
        self.write_status(seconds_ago=900)

        response = self._post(server_ip="")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(SwarmNetwork.objects.exists())
        self.assertIn("make overlay", response.context["form_error"])

    def test_empty_field_with_disconnected_overlay_is_refused(self):
        self.write_status(ip=None, connected=False, state="NeedsLogin")

        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(SwarmNetwork.objects.exists())
        self.assertIn(
            "NetBird is not connected (NeedsLogin)", response.context["form_error"]
        )

    def test_local_test_networks_ignore_the_field(self):
        with patch(
            "network.services.create_local_test_network", return_value=None
        ) as local_test:
            response = self._post(creation_method="local_test", server_ip="not an ip")

        self.assertEqual(response.status_code, 302)
        local_test.assert_called_once()


class CreateNetworkServiceTests(OverlayWebTestCase):
    def _create(self, server_ip=None):
        return network_services.create_network(
            project=self.project,
            actor=self.user,
            name="Service Network",
            participants=[{"name": "site-a", "ip": "100.64.0.11"}],
            server_ip=server_ip,
        )

    def test_invalid_explicit_address_creates_nothing(self):
        self.write_status()
        for value in (
            "host.example",
            "999.1.1.1",
            "127.0.0.1",
            "2001:db8::10",
            "Not Available",
        ):
            with self.subTest(server_ip=value):
                with self.assertRaises(ValueError):
                    self._create(value)
                self.assertFalse(SwarmNetwork.objects.exists())
                self.assertFalse(SwarmParticipant.objects.exists())

    def test_no_address_and_no_overlay_creates_nothing(self):
        with self.assertRaisesMessage(ValueError, "--server-ip"):
            self._create()
        self.assertFalse(SwarmNetwork.objects.exists())
        self.generate_kits.assert_not_called()

    def test_overlay_address_and_local_client(self):
        self.write_status()

        network = self._create()

        participants = dict(network.participants.values_list("participant_id", "ip"))
        self.assertEqual(
            participants,
            {
                "server": "100.64.0.10",
                "site-a": "100.64.0.11",
                "hub-host": "100.64.0.10",
            },
        )


class CLIServerAddressTests(OverlayWebTestCase):
    def _run(self, *args):
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = cli_main(
                [
                    "network",
                    "create",
                    "--user",
                    "alice",
                    "--project",
                    str(self.project.identifier),
                    "--json",
                    "--name",
                    "CLI Network",
                    "--participant",
                    "site-a=100.64.0.11",
                    *args,
                ]
            )
        return code, json.loads(stdout.getvalue())

    def test_invalid_server_ip_is_an_error(self):
        self.write_status()
        for value in ("fl-server.example.org", "999.1.1.1", "100.64.0.10/16", "::1"):
            with self.subTest(server_ip=value):
                code, payload = self._run("--server-ip", value)
                self.assertEqual(code, 2)
                self.assertFalse(payload["ok"])
                self.assertIn(
                    "cannot be used as the FL server address", payload["errors"][0]
                )
                self.assertFalse(SwarmNetwork.objects.exists())

    def test_missing_server_ip_without_overlay_is_an_error(self):
        code, payload = self._run()

        self.assertEqual(code, 2)
        self.assertIn("--server-ip", payload["errors"][0])
        self.assertFalse(SwarmNetwork.objects.exists())

    def test_missing_server_ip_uses_the_overlay_address(self):
        self.write_status()

        code, payload = self._run()

        self.assertEqual(code, 0, payload)
        network = SwarmNetwork.objects.get(identifier=payload["result"]["identifier"])
        self.assertEqual(network.participants.get(role="SERVER").ip, "100.64.0.10")
        self.assertEqual(payload["warnings"], [])

    def test_address_that_is_not_this_hosts_gives_a_warning(self):
        self.write_status()

        code, payload = self._run("--server-ip", "10.90.0.1", "--no-local-client")

        self.assertEqual(code, 0, payload)
        self.assertEqual(len(payload["warnings"]), 1)
        self.assertIn("SWARMMEDHUB_LOCAL_ADDRESSES", payload["warnings"][0])
