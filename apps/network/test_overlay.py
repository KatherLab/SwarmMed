"""Tests for reading this host's overlay status (network.overlay).

The status files are written into a temporary BASE_DIR; nothing reads the
host's real NetBird or Tailscale state. Addresses are documentation examples.
"""

import importlib.util
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import overlay
from .utils import get_tailscale_ip, is_tailscale_connected

TESTDATA = Path(__file__).resolve().parent / "testdata" / "overlay"
# The repository root; settings.BASE_DIR is overridden in these tests.
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_KEYS = (
    "SWARMMEDHUB_OVERLAY",
    "SWARMMEDHUB_LOCAL_ADDRESSES",
    "SWARMMEDHUB_OVERLAY_STALE_SECONDS",
)


def _iso(seconds_ago=0.0):
    moment = datetime.fromtimestamp(time.time() - seconds_ago, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _provider(ip=None, connected=None, state=None, installed=True, peers=None):
    return {
        "installed": installed,
        "ip": ip,
        "connected": connected,
        "state": state,
        "peers": peers,
        "error": None,
    }


def _status(provider="netbird", providers=None, seconds_ago=0.0, interval=30):
    """A status document as scripts/overlay_watcher.py writes it."""
    if providers is None:
        providers = {
            "netbird": _provider(
                "100.64.0.10", True, "Connected", peers={"connected": 3, "total": 3}
            ),
            "tailscale": _provider(installed=False),
        }
    top = providers.get(provider) or _provider(installed=None)
    return {
        "schema": 1,
        "provider": provider,
        **top,
        "updated_at": _iso(seconds_ago),
        "interval_seconds": interval,
        "providers": providers,
    }


class OverlayTestCase(SimpleTestCase):
    """A temporary BASE_DIR with tmp/, a clean environment and fresh warnings."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.base = Path(temp_dir.name)
        (self.base / "tmp").mkdir()
        settings_override = override_settings(BASE_DIR=str(self.base))
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        overlay.reset_warnings()
        self.addCleanup(overlay.reset_warnings)

    def write_status(self, payload, mtime_seconds_ago=None):
        path = self.base / "tmp" / "overlay_status.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        if mtime_seconds_ago is not None:
            stamp = time.time() - mtime_seconds_ago
            os.utime(path, (stamp, stamp))
        return path

    def write_legacy(
        self, ipv4="100.64.0.7", connected="true", state="Running", seconds_ago=0.0
    ):
        """The old scripts/tailscale_watcher.sh's file (strings, no timestamp)."""
        path = self.base / "tmp" / "tailscale_status.json"
        path.write_text(
            json.dumps({"ipv4": ipv4, "backend_state": state, "connected": connected})
        )
        stamp = time.time() - seconds_ago
        os.utime(path, (stamp, stamp))
        return path


class StatusFileTests(OverlayTestCase):
    def test_auto_uses_the_helpers_choice(self):
        self.write_status(_status("netbird"))

        status = overlay.get_overlay_status()

        self.assertEqual(status.mode, "auto")
        self.assertEqual(status.provider, "netbird")
        self.assertEqual(status.provider_label, "NetBird")
        self.assertEqual(status.ip, "100.64.0.10")
        self.assertIs(status.connected, True)
        self.assertEqual(status.state, "Connected")
        self.assertEqual(status.peers, {"connected": 3, "total": 3})
        self.assertFalse(status.stale)
        self.assertEqual(status.source, "status-file")
        self.assertEqual(status.connection, "connected")
        self.assertEqual(overlay.get_overlay_ip(), "100.64.0.10")
        self.assertEqual(overlay.is_overlay_connected(), "connected")
        self.assertEqual(status.current_address, "100.64.0.10")
        self.assertEqual(status.peers_text, "3 of 3 peers connected")

    def test_explicit_mode_uses_its_provider_entry_over_the_top_level(self):
        providers = {
            "netbird": _provider("100.64.0.10", True, "Connected"),
            "tailscale": _provider(
                "100.64.0.7", True, "Running", peers={"connected": 2, "total": 3}
            ),
        }
        self.write_status(_status("netbird", providers))

        os.environ["SWARMMEDHUB_OVERLAY"] = "tailscale"
        status = overlay.get_overlay_status()
        self.assertEqual((status.provider, status.ip), ("tailscale", "100.64.0.7"))
        self.assertEqual(status.peers_text, "2 of 3 peers online")

        os.environ["SWARMMEDHUB_OVERLAY"] = "netbird"
        status = overlay.get_overlay_status()
        self.assertEqual((status.provider, status.ip), ("netbird", "100.64.0.10"))

    def test_mode_is_case_insensitive_and_unknown_values_mean_auto(self):
        self.write_status(_status("netbird"))
        os.environ["SWARMMEDHUB_OVERLAY"] = " NetBird "
        self.assertEqual(overlay.overlay_mode(), "netbird")

        os.environ["SWARMMEDHUB_OVERLAY"] = "zerotier"
        with self.assertLogs("network.overlay", level="WARNING") as logs:
            self.assertEqual(overlay.overlay_mode(), "auto")
            self.assertEqual(overlay.overlay_mode(), "auto")
        self.assertEqual(len(logs.records), 1)  # warned once

    def test_tailscale_mode_ignores_a_netbird_only_file_without_providers(self):
        payload = _status("netbird")
        del payload["providers"]
        self.write_status(payload)
        os.environ["SWARMMEDHUB_OVERLAY"] = "tailscale"

        status = overlay.get_overlay_status()

        self.assertEqual(status.source, "none")
        self.assertIsNone(status.ip)
        self.assertEqual(status.connection, overlay.NOT_AVAILABLE)
        self.assertEqual(overlay.get_overlay_ip(), overlay.NOT_AVAILABLE)
        # Its address still identifies this host.
        self.assertEqual(overlay.get_local_addresses(), frozenset({"100.64.0.10"}))

    def test_file_without_providers_counts_when_its_provider_is_the_mode(self):
        payload = _status(
            "tailscale", {"tailscale": _provider("100.64.0.7", True, "Running")}
        )
        del payload["providers"]
        self.write_status(payload)
        os.environ["SWARMMEDHUB_OVERLAY"] = "tailscale"

        self.assertEqual(overlay.get_overlay_ip(), "100.64.0.7")

    def test_provider_not_installed(self):
        self.write_status(_status("netbird", {"netbird": _provider(installed=False)}))
        os.environ["SWARMMEDHUB_OVERLAY"] = "netbird"

        status = overlay.get_overlay_status()

        self.assertEqual(status.connection, overlay.NOT_AVAILABLE)
        self.assertIsNone(status.current_address)
        self.assertIn("no NetBird or Tailscale client", status.unavailable_reason())

    def test_helper_found_no_overlay(self):
        self.write_status(_status("none", {}))

        status = overlay.get_overlay_status()

        self.assertEqual(status.provider, "none")
        self.assertEqual(status.connection, overlay.NOT_AVAILABLE)
        self.assertEqual(
            status.summary, "No NetBird or Tailscale client found on this host"
        )

    def test_addresses_that_are_not_plain_ipv4_are_ignored(self):
        for value in ("100.64.0.10/16", "host.example", "::1", "999.1.1.1", 42):
            with self.subTest(ip=value):
                providers = {"netbird": _provider(value, True, "Connected")}
                self.write_status(_status("netbird", providers))
                status = overlay.get_overlay_status()
                self.assertIsNone(status.ip)
                self.assertEqual(status.addresses, frozenset())
                self.assertIsNone(status.current_address)
                self.assertIn("no IPv4 address", status.unavailable_reason())

    def test_invalid_json_is_reported_not_raised(self):
        self.write_status("{not json")

        status = overlay.get_overlay_status()

        self.assertEqual(status.source, "none")
        self.assertIn("not valid JSON", status.error)
        self.assertEqual(overlay.get_overlay_ip(), overlay.NOT_AVAILABLE)
        self.assertEqual(status.summary, "The overlay helper's status cannot be read")

    def test_no_file_at_all(self):
        status = overlay.get_overlay_status()

        self.assertEqual(status.source, "none")
        self.assertEqual(status.connection, overlay.NOT_AVAILABLE)
        self.assertEqual(status.summary, "No status from the overlay helper")
        self.assertIn("make overlay", status.unavailable_reason())

    def test_peer_counts_must_be_counts(self):
        providers = {
            "netbird": _provider(
                "100.64.0.10", True, "Connected", peers={"connected": "3", "total": 3}
            )
        }
        self.write_status(_status("netbird", providers))

        self.assertIsNone(overlay.get_overlay_status().peers)


class StaleStatusTests(OverlayTestCase):
    def test_stale_by_updated_at(self):
        self.write_status(_status("netbird", seconds_ago=7 * 60))

        status = overlay.get_overlay_status()

        self.assertTrue(status.stale)
        self.assertEqual(status.connection, "stale")
        self.assertEqual(status.age_text, "7 min")
        self.assertIn("Stale", status.summary)
        # The last known address is still this host's address ...
        self.assertEqual(overlay.get_overlay_ip(), "100.64.0.10")
        self.assertEqual(overlay.get_local_addresses(), frozenset({"100.64.0.10"}))
        # ... but new networks need an explicit server address.
        self.assertIsNone(status.current_address)
        with self.assertRaisesMessage(ValueError, "make overlay"):
            overlay.resolve_server_address("")

    def test_stale_warning_is_rate_limited(self):
        self.write_status(_status("netbird", seconds_ago=600))
        with self.assertLogs("network.overlay", level="WARNING") as logs:
            overlay.get_overlay_status()
            overlay.get_overlay_status()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("100.64.0.10", logs.output[0])

    def test_fresh_within_the_threshold(self):
        self.write_status(_status("netbird", seconds_ago=90))
        self.assertFalse(overlay.get_overlay_status().stale)

    def test_threshold_from_environment(self):
        self.write_status(_status("netbird", seconds_ago=90))
        os.environ["SWARMMEDHUB_OVERLAY_STALE_SECONDS"] = "60"
        self.assertTrue(overlay.get_overlay_status().stale)

    def test_invalid_threshold_falls_back_to_the_default(self):
        self.write_status(_status("netbird", seconds_ago=90))
        os.environ["SWARMMEDHUB_OVERLAY_STALE_SECONDS"] = "-5"
        with self.assertLogs("network.overlay", level="WARNING"):
            self.assertFalse(overlay.get_overlay_status().stale)

    def test_a_longer_helper_interval_extends_the_threshold(self):
        # Three intervals of 60 s: 150 s old is still current.
        self.write_status(_status("netbird", seconds_ago=150, interval=60))
        self.assertFalse(overlay.get_overlay_status().stale)

    def test_the_helper_interval_is_capped(self):
        # The interval comes from the file; a huge value must not keep a day-old
        # address fresh. Three capped intervals (600 s) are 30 minutes.
        self.write_status(_status("netbird", seconds_ago=29 * 60, interval=1e9))
        self.assertFalse(overlay.get_overlay_status().stale)
        self.write_status(_status("netbird", seconds_ago=24 * 3600, interval=1e9))
        status = overlay.get_overlay_status()
        self.assertTrue(status.stale)
        with self.assertRaisesMessage(ValueError, "make overlay"):
            overlay.resolve_server_address("")

    def test_age_from_the_file_time_when_the_timestamp_is_missing(self):
        payload = _status("netbird")
        payload["updated_at"] = "not a time"
        self.write_status(payload, mtime_seconds_ago=300)

        status = overlay.get_overlay_status()

        self.assertTrue(status.stale)
        self.assertGreaterEqual(status.age_seconds, 299)

    def test_a_time_up_to_a_minute_ahead_counts_as_now(self):
        payload = _status("netbird")
        payload["updated_at"] = _iso(-30)
        self.write_status(payload)

        status = overlay.get_overlay_status()

        self.assertFalse(status.stale)
        self.assertEqual(status.age_seconds, 0.0)
        self.assertIsNone(status.ahead_seconds)
        self.assertEqual(overlay.resolve_server_address(None), "100.64.0.10")

    def test_a_time_further_in_the_future_makes_the_status_stale(self):
        # The helper wrote while the clock was a day ahead, the clock was set
        # back, and the helper stopped an hour ago: the status's age is unknown.
        payload = _status("netbird")
        payload["updated_at"] = _iso(-(86400 + 600))
        self.write_status(payload, mtime_seconds_ago=3600)

        with self.assertLogs("network.overlay", level="WARNING") as logs:
            status = overlay.get_overlay_status()

        self.assertIn("in the future", logs.output[0])
        self.assertTrue(status.stale)
        self.assertEqual(status.connection, "stale")
        self.assertIsNone(status.age_seconds)
        self.assertAlmostEqual(status.ahead_seconds, 87000, delta=60)
        self.assertEqual(
            status.summary,
            "Stale: the overlay helper's last status is dated 24 h in the future",
        )
        self.assertEqual(status.peers_text, "")
        # The address still identifies this host, but is not a server address.
        self.assertEqual(overlay.get_local_addresses(), frozenset({"100.64.0.10"}))
        self.assertIsNone(status.current_address)
        with self.assertRaisesMessage(ValueError, "24 h in the future"):
            overlay.resolve_server_address(None)

    def test_a_time_outside_the_datetime_range_falls_back_to_the_file_time(self):
        for stamp in ("0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"):
            with self.subTest(updated_at=stamp):
                payload = _status("netbird")
                payload["updated_at"] = stamp
                self.write_status(payload, mtime_seconds_ago=10)

                status = overlay.get_overlay_status()

                self.assertFalse(status.stale)
                self.assertGreaterEqual(status.age_seconds, 9)
                self.assertEqual(status.ip, "100.64.0.10")


class LegacyFileTests(OverlayTestCase):
    def test_legacy_file_alone_is_read_as_tailscale(self):
        self.write_legacy(connected="true")

        status = overlay.get_overlay_status()

        self.assertEqual(status.source, "legacy-file")
        self.assertEqual((status.provider, status.ip), ("tailscale", "100.64.0.7"))
        self.assertIs(status.connected, True)
        self.assertEqual(status.state, "Running")
        self.assertEqual(status.current_address, "100.64.0.7")

    def test_legacy_strings_and_booleans(self):
        for raw, expected in (
            ("true", True),
            ("false", False),
            (True, True),
            (False, False),
        ):
            with self.subTest(connected=raw):
                self.write_legacy(connected=raw)
                self.assertIs(overlay.get_overlay_status().connected, expected)

    def test_legacy_empty_address_means_none(self):
        self.write_legacy(ipv4="", connected="false", state="Unknown")

        status = overlay.get_overlay_status()

        self.assertIsNone(status.ip)
        self.assertEqual(status.connection, "disconnected")
        self.assertEqual(overlay.get_local_addresses(), frozenset())

    def test_legacy_file_is_stale_by_modification_time(self):
        self.write_legacy(seconds_ago=600)
        self.assertTrue(overlay.get_overlay_status().stale)

    def test_legacy_file_is_not_used_in_netbird_mode(self):
        self.write_legacy()
        os.environ["SWARMMEDHUB_OVERLAY"] = "netbird"

        status = overlay.get_overlay_status()

        self.assertEqual(status.source, "none")
        self.assertEqual(status.addresses, frozenset({"100.64.0.7"}))

    def test_fresh_new_file_wins_over_a_fresh_legacy_file(self):
        self.write_legacy(seconds_ago=0)
        self.write_status(_status("netbird", seconds_ago=20))

        status = overlay.get_overlay_status()

        self.assertEqual((status.source, status.ip), ("status-file", "100.64.0.10"))
        self.assertEqual(status.addresses, frozenset({"100.64.0.10", "100.64.0.7"}))

    def test_fresh_legacy_file_wins_over_a_stale_new_file(self):
        self.write_status(_status("netbird", seconds_ago=900))
        self.write_legacy(seconds_ago=10)

        status = overlay.get_overlay_status()

        self.assertEqual((status.source, status.ip), ("legacy-file", "100.64.0.7"))
        self.assertFalse(status.stale)

    def test_when_both_are_stale_the_more_recent_counts(self):
        self.write_status(_status("netbird", seconds_ago=900))
        self.write_legacy(seconds_ago=600)
        self.assertEqual(overlay.get_overlay_status().source, "legacy-file")

        self.write_status(_status("netbird", seconds_ago=300))
        status = overlay.get_overlay_status()
        self.assertEqual(status.source, "status-file")
        self.assertTrue(status.stale)

    def test_a_stale_legacy_file_older_than_the_new_status_no_longer_counts(self):
        # The old helper stopped an hour ago; the new one has written since, and
        # its own status (stale or not) no longer lists the old address.
        self.write_legacy(ipv4="100.64.0.7", seconds_ago=3600)
        for new_age in (0, 900):
            with self.subTest(new_status_age=new_age):
                self.write_status(_status("netbird", seconds_ago=new_age))

                status = overlay.get_overlay_status()

                self.assertEqual(status.source, "status-file")
                self.assertEqual(status.addresses, frozenset({"100.64.0.10"}))
                self.assertNotIn("100.64.0.7", overlay.get_local_addresses())

    def test_a_stale_legacy_file_counts_while_no_newer_status_exists(self):
        self.write_legacy(ipv4="100.64.0.7", seconds_ago=3600)
        self.assertEqual(
            overlay.get_overlay_status().addresses, frozenset({"100.64.0.7"})
        )

        # The new helper's status is older still: the old file stays the newest.
        self.write_status(_status("netbird", seconds_ago=7200))
        self.assertEqual(
            overlay.get_overlay_status().addresses,
            frozenset({"100.64.0.10", "100.64.0.7"}),
        )


class LocalAddressTests(OverlayTestCase):
    def test_every_overlay_address_identifies_this_host(self):
        providers = {
            "netbird": _provider("100.64.0.10", True, "Connected"),
            "tailscale": _provider("100.64.0.7", False, "Stopped"),
        }
        self.write_status(_status("netbird", providers))
        os.environ["SWARMMEDHUB_LOCAL_ADDRESSES"] = "10.90.0.1, not-an-ip, 10.90.0.1"

        with self.assertLogs("network.overlay", level="WARNING"):
            addresses = overlay.get_local_addresses()

        self.assertEqual(
            addresses, frozenset({"100.64.0.10", "100.64.0.7", "10.90.0.1"})
        )
        # The status itself is still NetBird's.
        self.assertEqual(overlay.get_overlay_ip(), "100.64.0.10")

    def test_mode_none_reads_no_file(self):
        self.write_status(_status("netbird"))
        self.write_legacy()
        os.environ["SWARMMEDHUB_OVERLAY"] = "none"

        status = overlay.get_overlay_status()

        self.assertEqual((status.provider, status.source), ("none", "none"))
        self.assertIsNone(status.ip)
        self.assertEqual(status.addresses, frozenset())
        self.assertEqual(status.connection, overlay.NOT_AVAILABLE)
        self.assertEqual(status.summary, "No overlay (SWARMMEDHUB_OVERLAY=none)")
        with self.assertRaisesMessage(ValueError, "SWARMMEDHUB_LOCAL_ADDRESSES"):
            overlay.resolve_server_address(None)

    def test_mode_none_uses_the_configured_addresses(self):
        os.environ["SWARMMEDHUB_OVERLAY"] = "none"
        os.environ["SWARMMEDHUB_LOCAL_ADDRESSES"] = "10.90.0.1,10.90.0.2"

        status = overlay.get_overlay_status()

        self.assertEqual((status.ip, status.source), ("10.90.0.1", "env"))
        self.assertEqual(status.addresses, frozenset({"10.90.0.1", "10.90.0.2"}))
        self.assertEqual(overlay.resolve_server_address(""), "10.90.0.1")

    def test_mode_none_skips_addresses_that_cannot_be_a_server_address(self):
        os.environ["SWARMMEDHUB_OVERLAY"] = "none"
        os.environ["SWARMMEDHUB_LOCAL_ADDRESSES"] = "127.0.0.1,10.90.0.1"

        status = overlay.get_overlay_status()

        self.assertEqual((status.ip, status.current_address), ("10.90.0.1",) * 2)
        self.assertEqual(status.addresses, frozenset({"127.0.0.1", "10.90.0.1"}))
        self.assertEqual(overlay.resolve_server_address(None), "10.90.0.1")

    def test_mode_none_without_a_usable_address_says_why(self):
        os.environ["SWARMMEDHUB_OVERLAY"] = "none"
        os.environ["SWARMMEDHUB_LOCAL_ADDRESSES"] = "127.0.0.1"

        status = overlay.get_overlay_status()

        self.assertEqual((status.ip, status.source), ("127.0.0.1", "env"))
        self.assertIsNone(status.current_address)
        with self.assertRaises(ValueError) as caught:
            overlay.resolve_server_address(None)
        message = str(caught.exception)
        self.assertIn(
            "SWARMMEDHUB_LOCAL_ADDRESSES names no address that other sites can "
            "connect to",
            message,
        )
        self.assertNotIn("NetBird", message)


class CompatibilityTests(OverlayTestCase):
    def test_old_names_return_the_overlay_values(self):
        self.write_status(_status("netbird"))
        self.assertEqual(get_tailscale_ip(), overlay.get_overlay_ip())
        self.assertEqual(get_tailscale_ip(), "100.64.0.10")
        self.assertEqual(is_tailscale_connected(), "connected")

    def test_old_connection_name_never_says_stale(self):
        self.write_status(_status("netbird", seconds_ago=900))
        self.assertEqual(overlay.is_overlay_connected(), "stale")
        self.assertEqual(is_tailscale_connected(), "disconnected")

    def test_old_names_without_any_status(self):
        self.assertEqual(get_tailscale_ip(), "Not Available")
        self.assertEqual(is_tailscale_connected(), "Not Available")


class ServerAddressTests(OverlayTestCase):
    def test_usable_server_addresses(self):
        for value in ("100.64.0.10", "10.90.0.1", "192.0.2.10", " 198.51.100.7 "):
            with self.subTest(value=value):
                self.assertTrue(overlay.is_usable_server_address(value))
        for value in (
            "",
            None,
            "0.0.0.0",  # nosec B104 - a test value, nothing binds to it
            "0.1.2.3",
            "0.255.255.255",
            "127.0.0.1",
            "169.254.10.1",
            "224.0.0.1",
            "240.0.0.1",
            "255.255.255.255",
            "999.1.1.1",
            "100.64.0.10/16",
            "100.064.0.10",
            "host.example",
            "server",
            "::1",
            "2001:db8::10",
        ):
            with self.subTest(value=value):
                self.assertFalse(overlay.is_usable_server_address(value))

    def test_explicit_address_is_used_without_any_overlay(self):
        self.assertEqual(overlay.resolve_server_address(" 10.90.0.1 "), "10.90.0.1")

    def test_explicit_address_must_be_usable(self):
        self.write_status(_status("netbird"))
        for value in ("host.example", "127.0.0.1", "100.64.0.10/16", "2001:db8::10"):
            with (
                self.subTest(value=value),
                self.assertRaisesMessage(
                    ValueError, "cannot be used as the FL server address"
                ),
            ):
                overlay.resolve_server_address(value)

    def test_overlay_address_when_none_is_given(self):
        self.write_status(_status("netbird"))
        self.assertEqual(overlay.resolve_server_address(None), "100.64.0.10")
        self.assertEqual(overlay.resolve_server_address("  "), "100.64.0.10")

    def test_disconnected_overlay_is_refused(self):
        providers = {"netbird": _provider(None, False, "NeedsLogin")}
        self.write_status(_status("netbird", providers))
        with self.assertRaisesMessage(
            ValueError, "NetBird is not connected (NeedsLogin)"
        ):
            overlay.resolve_server_address(None)

    def test_no_status_is_refused(self):
        with self.assertRaisesMessage(ValueError, "--server-ip"):
            overlay.resolve_server_address(None)

    def test_interface_only_address_can_be_used(self):
        # The helper could not reach the NetBird daemon but read the interface.
        providers = {"netbird": _provider("100.64.0.10", None, "interface-only")}
        self.write_status(_status("netbird", providers))

        status = overlay.get_overlay_status()

        self.assertEqual(status.connection, "disconnected")
        self.assertEqual(status.summary, "Connection state unknown (interface-only)")
        self.assertEqual(overlay.resolve_server_address(None), "100.64.0.10")


def _load_watcher():
    path = REPO_ROOT / "scripts" / "overlay_watcher.py"
    spec = importlib.util.spec_from_file_location("overlay_watcher_for_reader", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WatcherRoundTripTests(OverlayTestCase):
    """The Hub reads what the helper writes."""

    def test_status_built_from_netbird_and_tailscale_samples(self):
        watcher = _load_watcher()
        netbird = watcher.parse_netbird_status(
            (TESTDATA / "netbird_connected_synthetic.json").read_text()
        )
        tailscale = watcher.parse_tailscale_status(
            (TESTDATA / "tailscale_running_synthetic.json").read_text()
        )
        providers = {
            "netbird": {"installed": True, **netbird},
            "tailscale": {"installed": True, **tailscale},
        }
        watcher.write_status(
            self.base / "tmp" / "overlay_status.json",
            watcher.build_status("auto", providers, 30),
        )

        status = overlay.get_overlay_status()
        self.assertEqual((status.provider, status.ip), ("netbird", "100.64.0.10"))
        self.assertEqual(status.peers, {"connected": 3, "total": 3})
        self.assertEqual(status.addresses, frozenset({"100.64.0.10", "100.64.0.7"}))

        os.environ["SWARMMEDHUB_OVERLAY"] = "tailscale"
        status = overlay.get_overlay_status()
        self.assertEqual((status.provider, status.ip), ("tailscale", "100.64.0.7"))
        self.assertEqual(status.peers, {"connected": 2, "total": 3})

    def test_auto_applies_the_hubs_preference_whatever_the_helper_chose(self):
        # scripts/tailscale_watcher.sh, which old cron entries and systemd
        # units still start, runs the helper with --provider tailscale.
        watcher = _load_watcher()

        def sample(parse, name):
            return {"installed": True, **parse((TESTDATA / name).read_text())}

        netbird_up = sample(
            watcher.parse_netbird_status, "netbird_connected_synthetic.json"
        )
        netbird_logged_out = sample(
            watcher.parse_netbird_status, "netbird_0.82.1_needs_login.json"
        )
        tailscale_up = sample(
            watcher.parse_tailscale_status, "tailscale_running_synthetic.json"
        )
        absent = watcher.empty_provider()
        cases = [
            # (NetBird, Tailscale) -> (the Hub's choice, its address)
            ((netbird_up, tailscale_up), ("netbird", "100.64.0.10")),
            ((netbird_logged_out, tailscale_up), ("tailscale", "100.64.0.7")),
            ((netbird_logged_out, absent), ("netbird", None)),
            ((absent, absent), ("none", None)),
        ]
        path = self.base / "tmp" / "overlay_status.json"
        for (netbird, tailscale), expected in cases:
            providers = {"netbird": netbird, "tailscale": tailscale}
            for helper_mode in ("tailscale", "netbird", "auto"):
                with self.subTest(expected=expected, helper=helper_mode):
                    watcher.write_status(
                        path, watcher.build_status(helper_mode, providers, 30)
                    )
                    status = overlay.get_overlay_status()
                    self.assertEqual((status.provider, status.ip), expected)

    def test_status_built_from_the_real_management_down_sample(self):
        watcher = _load_watcher()
        netbird = watcher.parse_netbird_status(
            (TESTDATA / "netbird_0.82.1_management_down.json").read_text()
        )
        providers = {
            "netbird": {"installed": True, **netbird},
            "tailscale": watcher.empty_provider(),
        }
        watcher.write_status(
            self.base / "tmp" / "overlay_status.json",
            watcher.build_status("auto", providers, 30),
        )

        status = overlay.get_overlay_status()
        self.assertEqual((status.provider, status.ip), ("netbird", "192.0.2.11"))
        self.assertEqual(status.connection, "connected")
        self.assertEqual(overlay.resolve_server_address(), "192.0.2.11")


def _real_status(name, seconds_ago=5.0):
    """A status file that the helper wrote in the containers-only NetBird test.

    Only its time stamp is moved, to ``seconds_ago`` before now.
    """
    payload = json.loads((TESTDATA / name).read_text())
    payload["updated_at"] = _iso(seconds_ago)
    return payload


class RealHelperOutputTests(OverlayTestCase):
    """Files written by scripts/overlay_watcher.py next to NetBird 0.82.1.

    Captured in a containers-only test with a self-hosted management server on
    11 Nov 2026; the peer's address was replaced by 192.0.2.13.
    """

    def test_connected(self):
        self.write_status(_real_status("overlay_status_netbird_0.82.1_connected.json"))

        status = overlay.get_overlay_status()

        self.assertEqual((status.provider, status.ip), ("netbird", "192.0.2.13"))
        self.assertIs(status.connected, True)
        self.assertEqual(status.connection, "connected")
        self.assertEqual(status.summary, "Connected")
        self.assertEqual(status.peers_text, "1 of 1 peers connected")
        self.assertFalse(status.stale)
        self.assertEqual(overlay.resolve_server_address(), "192.0.2.13")
        self.assertEqual(overlay.get_local_addresses(), frozenset({"192.0.2.13"}))

    def test_management_down_keeps_the_address_usable(self):
        self.write_status(
            _real_status("overlay_status_netbird_0.82.1_management_down.json")
        )

        status = overlay.get_overlay_status()

        self.assertEqual(status.connection, "connected")
        self.assertEqual(status.summary, "Connected")
        self.assertTrue(status.error.startswith("management: rpc error"))
        self.assertEqual(overlay.resolve_server_address(), "192.0.2.13")

    def test_logged_out(self):
        self.write_status(_real_status("overlay_status_netbird_0.82.1_logged_out.json"))

        status = overlay.get_overlay_status()

        self.assertIsNone(status.ip)
        self.assertEqual(status.connection, "disconnected")
        self.assertEqual(status.summary, "Not connected (NeedsLogin)")
        # NetBird lists 0 of 0 peers while logged out; the page leaves that out.
        self.assertEqual(status.peers, {"connected": 0, "total": 0})
        self.assertEqual(status.peers_text, "")
        self.assertEqual(
            status.unavailable_reason(), "NetBird is not connected (NeedsLogin)"
        )
        with self.assertRaisesMessage(ValueError, "NetBird is not connected"):
            overlay.resolve_server_address()
        self.assertEqual(overlay.get_local_addresses(), frozenset())

    def test_connecting(self):
        self.write_status(_real_status("overlay_status_netbird_0.82.1_connecting.json"))

        status = overlay.get_overlay_status()

        self.assertEqual(status.summary, "Not connected (Connecting)")
        self.assertIn("FailedPrecondition", status.error)
        with self.assertRaisesMessage(ValueError, "NetBird is not connected"):
            overlay.resolve_server_address()

    def test_original_time_stamp_makes_the_file_stale(self):
        self.write_status(
            (TESTDATA / "overlay_status_netbird_0.82.1_connected.json").read_text()
        )

        with self.assertLogs("network.overlay", level="WARNING"):
            status = overlay.get_overlay_status()

        self.assertEqual(
            status.updated_at, datetime(2026, 11, 11, 10, 28, 42, tzinfo=UTC)
        )
        self.assertEqual(status.connection, "stale")
        with self.assertRaisesMessage(ValueError, "make overlay"):
            overlay.resolve_server_address()
        # The last known address still identifies this host.
        self.assertEqual(overlay.get_local_addresses(), frozenset({"192.0.2.13"}))
