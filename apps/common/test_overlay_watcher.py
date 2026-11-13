"""Tests for the host-side overlay helper, scripts/overlay_watcher.py.

The NetBird samples in apps/network/testdata/overlay/ are partly real and
partly synthetic. The real ones come from NetBird 0.82.1: logged out and idle
from an isolated container, and connected, management server down, logged out
after a logout and reconnecting from a containers-only test with a self-hosted
management server (11 Nov 2026; addresses and keys replaced, see the README
there). The synthetic ones are built from the JSON structs of NetBird's
client/status/status.go. Other tests run the script with fake ``netbird``,
``tailscale`` and ``ip`` programs on PATH, so the host's own overlay clients
are never called.
"""

import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess  # nosec B404
import sys
import tempfile
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from network import overlay

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "overlay_watcher.py"
WRAPPER = REPO_ROOT / "scripts" / "tailscale_watcher.sh"
TESTDATA = REPO_ROOT / "apps" / "network" / "testdata" / "overlay"

# What NetBird 0.82.1 printed when no daemon listened (probe of 11 Nov 2026,
# shortened); it exits 1 after about 10 s of retries.
NETBIRD_NO_DAEMON_STDERR = (
    "2026-11-11T09:25:10.003Z INFO ./caller_not_available:0: WARNING: [core] "
    "grpc: addrConn.createTransport failed to connect\n"
    "Error: failed to connect to daemon error: context deadline exceeded\n"
    "If the daemon is not running please run: \n"
    "netbird service install \n"
    "netbird service start\n"
)
WT0_LINE = (
    "4: wt0    inet 100.64.0.10/16 brd 100.64.255.255 scope global wt0\\       "
    "valid_lft forever preferred_lft forever"
)


def _load_watcher():
    spec = importlib.util.spec_from_file_location("overlay_watcher_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample(name):
    return (TESTDATA / name).read_text()


class ParseNetBirdTests(SimpleTestCase):
    def setUp(self):
        self.watcher = _load_watcher()

    def test_connected_sample(self):
        result = self.watcher.parse_netbird_status(
            _sample("netbird_connected_synthetic.json")
        )

        self.assertEqual(
            result,
            {
                "ip": "100.64.0.10",  # "100.64.0.10/16" without its prefix length
                "connected": True,
                "state": "Connected",
                "peers": {"connected": 3, "total": 3},
                "error": None,
            },
        )
        # Other peers' names and addresses are not kept.
        text = json.dumps(result)
        self.assertNotIn("100.64.0.11", text)
        self.assertNotIn("fl-client-1", text)

    def test_needs_login_sample_from_netbird_0_82_1(self):
        result = self.watcher.parse_netbird_status(
            _sample("netbird_0.82.1_needs_login.json")
        )

        self.assertIsNone(result["ip"])
        self.assertIs(result["connected"], False)
        self.assertEqual(result["state"], "NeedsLogin")
        self.assertEqual(result["peers"], {"connected": 0, "total": 0})
        self.assertIsNone(result["error"])

    def test_idle_sample_from_netbird_0_82_1(self):
        result = self.watcher.parse_netbird_status(_sample("netbird_0.82.1_idle.json"))

        self.assertEqual((result["state"], result["connected"]), ("Idle", False))
        self.assertIsNone(result["ip"])

    def test_connected_with_peers_sample_from_netbird_0_82_1(self):
        result = self.watcher.parse_netbird_status(
            _sample("netbird_0.82.1_connected_with_peers.json")
        )

        self.assertEqual(
            result,
            {
                "ip": "192.0.2.10",  # "192.0.2.10/16" without its prefix length
                "connected": True,
                "state": "Connected",
                "peers": {"connected": 2, "total": 2},
                "error": None,
            },
        )
        # Other peers' names and addresses, and the relay's, are not kept.
        text = json.dumps(result)
        for other in ("192.0.2.11", "192.0.2.12", "fl-client-1", "nb-server.test"):
            self.assertNotIn(other, text)

    def test_management_down_sample_from_netbird_0_82_1(self):
        # Observed: the tunnel and the address stay while the management
        # server is down, so the peer still counts as connected, with the error.
        result = self.watcher.parse_netbird_status(
            _sample("netbird_0.82.1_management_down.json")
        )

        self.assertEqual(result["ip"], "192.0.2.11")
        self.assertIs(result["connected"], True)
        self.assertEqual(result["state"], "Connected")
        self.assertEqual(result["peers"], {"connected": 1, "total": 1})
        self.assertEqual(
            result["error"],
            "management: rpc error: code = Internal desc = server closed the "
            "stream without sending trailers",
        )

    def test_after_logout_sample_from_netbird_0_82_1(self):
        result = self.watcher.parse_netbird_status(
            _sample("netbird_0.82.1_after_logout.json")
        )

        self.assertEqual(
            result,
            {
                "ip": None,
                "connected": False,
                "state": "NeedsLogin",
                "peers": {"connected": 0, "total": 0},
                "error": None,
            },
        )

    def test_restarted_while_management_down_sample_from_netbird_0_82_1(self):
        result = self.watcher.parse_netbird_status(
            _sample("netbird_0.82.1_restarted_management_down.json")
        )

        self.assertEqual((result["state"], result["connected"]), ("Connecting", False))
        self.assertIsNone(result["ip"])
        # NetBird had not reported an error yet (management.error was empty).
        self.assertIsNone(result["error"])

    def test_every_netbird_sample_parses(self):
        for path in sorted(TESTDATA.glob("netbird_*.json")):
            with self.subTest(sample=path.name):
                result = self.watcher.parse_netbird_status(path.read_text())
                self.assertIsNotNone(result)
                self.assertEqual(
                    set(result), {"ip", "connected", "state", "peers", "error"}
                )

    def test_connecting_sample(self):
        result = self.watcher.parse_netbird_status(
            _sample("netbird_connecting_synthetic.json")
        )

        self.assertEqual((result["state"], result["connected"]), ("Connecting", False))
        self.assertIsNone(result["ip"])
        self.assertTrue(result["error"].startswith("management: rpc error"))

    def test_without_daemon_status_management_decides(self):
        data = json.loads(_sample("netbird_connected_synthetic.json"))
        del data["daemonStatus"]
        result = self.watcher.parse_netbird_status(json.dumps(data))
        self.assertIs(result["connected"], True)
        self.assertIsNone(result["state"])

        data["management"]["connected"] = False
        result = self.watcher.parse_netbird_status(json.dumps(data))
        self.assertIs(result["connected"], False)

        del data["management"]
        result = self.watcher.parse_netbird_status(json.dumps(data))
        self.assertIsNone(result["connected"])

    def test_connected_while_management_is_lost(self):
        data = json.loads(_sample("netbird_connected_synthetic.json"))
        data["management"]["connected"] = False
        result = self.watcher.parse_netbird_status(json.dumps(data))

        self.assertIs(result["connected"], True)
        self.assertEqual(result["ip"], "100.64.0.10")
        self.assertEqual(result["error"], "management server not connected")

    def test_address_forms(self):
        parse = self.watcher.parse_ipv4
        self.assertEqual(parse("100.64.0.10/16"), "100.64.0.10")
        self.assertEqual(parse(" 100.64.0.10 "), "100.64.0.10")
        self.assertIsNone(parse("fd00::100/64"))
        self.assertIsNone(parse("fl-server.netbird.selfhosted"))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse(None))

    def test_output_that_is_not_a_json_object(self):
        for text in ("", "Daemon status: NeedsLogin", "[]", "null"):
            with self.subTest(text=text):
                self.assertIsNone(self.watcher.parse_netbird_status(text))


class ParseTailscaleTests(SimpleTestCase):
    def setUp(self):
        self.watcher = _load_watcher()

    def test_running_sample(self):
        result = self.watcher.parse_tailscale_status(
            _sample("tailscale_running_synthetic.json")
        )

        self.assertEqual(
            result,
            {
                "ip": "100.64.0.7",
                "connected": True,
                "state": "Running",
                "peers": {"connected": 2, "total": 3},  # "Online" peers
                "error": None,
            },
        )
        self.assertNotIn("100.64.0.1", json.dumps(result))

    def test_needs_login_sample(self):
        result = self.watcher.parse_tailscale_status(
            _sample("tailscale_needs_login_synthetic.json")
        )

        self.assertEqual((result["state"], result["connected"]), ("NeedsLogin", False))
        self.assertIsNone(result["ip"])
        self.assertEqual(result["peers"], {"connected": 0, "total": 0})

    def test_top_level_addresses_when_self_has_none(self):
        data = json.loads(_sample("tailscale_running_synthetic.json"))
        del data["Self"]
        data["TailscaleIPs"] = ["2001:db8::7", "100.64.0.7"]

        result = self.watcher.parse_tailscale_status(json.dumps(data))

        self.assertEqual(result["ip"], "100.64.0.7")


class ProbeTests(SimpleTestCase):
    """The probes, with the commands replaced."""

    def setUp(self):
        self.watcher = _load_watcher()
        self.env = {
            "NETBIRD_BIN": "/opt/fake/netbird",
            "TAILSCALE_BIN": "/opt/fake/tailscale",
        }

    def _probe_netbird(self, result, interface_address=None):
        with (
            patch.object(self.watcher, "run_command", return_value=result),
            patch.object(
                self.watcher, "interface_ipv4", return_value=interface_address
            ) as interface,
        ):
            record = self.watcher.probe_netbird(self.env)
        return record, interface

    def test_daemon_not_running(self):
        record, _ = self._probe_netbird((1, "", NETBIRD_NO_DAEMON_STDERR))

        self.assertEqual(
            record,
            {
                "installed": True,
                "ip": None,
                "connected": False,
                "state": "daemon-unavailable",
                "peers": None,
                "error": (
                    "Error: failed to connect to daemon error: context deadline "
                    "exceeded"
                ),
            },
        )

    def test_timeout(self):
        record, _ = self._probe_netbird(
            (None, "", "netbird did not answer within 20 s")
        )
        self.assertEqual(record["state"], "daemon-unavailable")
        self.assertEqual(record["error"], "netbird did not answer within 20 s")

    def test_daemon_not_running_but_the_interface_has_an_address(self):
        record, interface = self._probe_netbird(
            (1, "", NETBIRD_NO_DAEMON_STDERR), interface_address="100.64.0.10"
        )

        interface.assert_called_once_with("wt0")
        self.assertEqual(record["ip"], "100.64.0.10")
        self.assertIsNone(record["connected"])
        self.assertEqual(record["state"], "interface-only")
        self.assertIn("address read from interface wt0", record["error"])

    def test_logged_out_does_not_use_the_interface(self):
        record, interface = self._probe_netbird(
            (0, _sample("netbird_0.82.1_needs_login.json"), ""),
            interface_address="100.64.0.10",
        )

        interface.assert_not_called()
        self.assertIsNone(record["ip"])
        self.assertEqual(record["state"], "NeedsLogin")

    def test_status_withheld_by_netbird_0_83(self):
        sample = _sample("netbird_0.83_status_withheld_synthetic.json")
        record, _ = self._probe_netbird(
            (0, sample, ""), interface_address="100.64.0.10"
        )

        self.assertEqual(record["ip"], "100.64.0.10")
        self.assertIs(record["connected"], True)
        self.assertEqual(record["state"], "Connected")
        self.assertIsNone(record["peers"])  # the zero counts are not real
        self.assertIn("read from interface wt0", record["error"])

        record, _ = self._probe_netbird((0, sample, ""), interface_address=None)
        self.assertIsNone(record["ip"])
        self.assertIn("as root or as the user who ran netbird up", record["error"])

    def test_interface_name_from_environment(self):
        self.env["SWARMMEDHUB_NETBIRD_INTERFACE"] = "nb0"
        _, interface = self._probe_netbird((1, "", "Error: x"))
        interface.assert_called_once_with("nb0")

    def test_not_installed(self):
        with patch.object(self.watcher.shutil, "which", return_value=None):
            self.assertEqual(
                self.watcher.probe_netbird({}), self.watcher.empty_provider()
            )

    def test_tailscale_falls_back_to_ip_command(self):
        calls = []

        def fake_run(argv, timeout=20):
            calls.append(argv[1:])
            if argv[1:] == ["status", "--json"]:
                return 0, _sample("tailscale_needs_login_synthetic.json"), ""
            return 0, "100.64.0.7\n", ""

        with patch.object(self.watcher, "run_command", side_effect=fake_run):
            record = self.watcher.probe_tailscale(self.env)

        self.assertEqual(calls, [["status", "--json"], ["ip", "-4"]])
        self.assertEqual(record["ip"], "100.64.0.7")
        self.assertIs(record["connected"], False)

    def test_tailscale_daemon_not_running(self):
        message = (
            "failed to connect to local tailscaled; it doesn't appear to be running "
            "(sudo systemctl start tailscaled ?)"
        )
        with patch.object(self.watcher, "run_command", return_value=(1, "", message)):
            record = self.watcher.probe_tailscale(self.env)

        self.assertEqual(record["state"], "daemon-unavailable")
        self.assertIsNone(record["ip"])
        self.assertEqual(record["error"], message)

    def test_interface_address_parsing(self):
        with (
            patch.object(self.watcher, "_ip_binary", return_value="/sbin/ip"),
            patch.object(
                self.watcher, "run_command", return_value=(0, WT0_LINE + "\n", "")
            ) as run,
        ):
            self.assertEqual(self.watcher.interface_ipv4("wt0"), "100.64.0.10")
        run.assert_called_once_with(
            ["/sbin/ip", "-4", "-o", "addr", "show", "dev", "wt0"], 5
        )


class ChooseAndWriteTests(SimpleTestCase):
    def setUp(self):
        self.watcher = _load_watcher()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.dir = Path(temp_dir.name)

    def _providers(self, netbird=None, tailscale=None):
        base = self.watcher.empty_provider()
        return {
            "netbird": {**base, **(netbird or {})},
            "tailscale": {**base, **(tailscale or {})},
        }

    def test_auto_preference(self):
        choose = self.watcher.choose_provider
        both = self._providers(
            {"installed": True, "connected": True},
            {"installed": True, "connected": True},
        )
        self.assertEqual(choose("auto", both), "netbird")

        tailscale_only_connected = self._providers(
            {"installed": True, "connected": False},
            {"installed": True, "connected": True},
        )
        self.assertEqual(choose("auto", tailscale_only_connected), "tailscale")

        none_connected = self._providers(
            {"installed": True, "connected": False},
            {"installed": True, "connected": False},
        )
        self.assertEqual(choose("auto", none_connected), "netbird")

        only_tailscale = self._providers(None, {"installed": True, "connected": False})
        self.assertEqual(choose("auto", only_tailscale), "tailscale")

        self.assertEqual(choose("auto", self._providers()), "none")
        self.assertEqual(choose("tailscale", both), "tailscale")
        self.assertEqual(choose("none", both), "none")

    def test_status_schema(self):
        providers = self._providers(
            {
                "installed": True,
                "ip": "100.64.0.10",
                "connected": True,
                "state": "Connected",
            }
        )
        status = self.watcher.build_status("auto", providers, 30, now=1_794_391_200)

        self.assertEqual(
            list(status),
            [
                "schema",
                "provider",
                "ip",
                "connected",
                "state",
                "peers",
                "installed",
                "error",
                "updated_at",
                "interval_seconds",
                "watcher_mode",
                "providers",
            ],
        )
        self.assertEqual(status["schema"], 1)
        self.assertEqual((status["provider"], status["ip"]), ("netbird", "100.64.0.10"))
        self.assertEqual(status["updated_at"], "2026-11-11T10:00:00Z")
        self.assertEqual(status["providers"], providers)
        json.dumps(status)  # serialisable

    def test_none_status(self):
        status = self.watcher.build_status("none", {}, 30)
        self.assertEqual(status["provider"], "none")
        self.assertIsNone(status["ip"])
        self.assertEqual(status["providers"], {})

    def test_atomic_write_replaces_the_file_with_mode_0644(self):
        target = self.dir / "tmp" / "overlay_status.json"
        self.watcher.write_status(target, {"schema": 1, "n": 1})
        self.watcher.write_status(target, {"schema": 1, "n": 2})

        self.assertEqual(json.loads(target.read_text())["n"], 2)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertEqual(
            [p.name for p in target.parent.iterdir()], ["overlay_status.json"]
        )

    def test_failed_write_keeps_the_old_file_and_leaves_no_temporary_file(self):
        target = self.dir / "overlay_status.json"
        self.watcher.write_status(target, {"schema": 1, "n": 1})

        with self.assertRaises(TypeError):
            self.watcher.write_status(target, {"schema": 1, "n": object()})

        self.assertEqual(json.loads(target.read_text())["n"], 1)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["overlay_status.json"])


def _start_watcher(output, *args, env=None):
    """Start the helper in its loop (provider none) and wait until it holds the lock.

    The helper prints "overlay helper started" once it has locked its PID file,
    so no test depends on how soon a new process is visible in /proc.
    """
    process = subprocess.Popen(  # nosec B603
        [
            sys.executable,
            "-I",
            str(SCRIPT),
            "--provider",
            "none",
            "--interval",
            "5",
            "--output",
            str(output),
            *args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env or {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    line = process.stdout.readline()
    if "overlay helper started" not in line:
        process.kill()
        process.wait(timeout=30)
        raise AssertionError(f"the helper did not start: {line!r}")
    return process


def _stop_process(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=30)
    process.stdout.close()


class PidFileTests(SimpleTestCase):
    def setUp(self):
        self.watcher = _load_watcher()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.base = Path(temp_dir.name)
        self.pid_path = self.base / "overlay_watcher.pid"

    def test_acquire_and_release(self):
        self.assertTrue(self.watcher.acquire_pid_file(self.pid_path))
        self.assertEqual(self.pid_path.read_text().strip(), str(os.getpid()))
        # The lock is held while the file is open, even within one process.
        self.assertEqual(self.watcher.running_watcher(self.pid_path), os.getpid())
        self.watcher.release_pid_file(self.pid_path)
        # Emptied, not removed, and no longer locked.
        self.assertEqual(self.pid_path.read_text(), "")
        self.assertIsNone(self.watcher.running_watcher(self.pid_path))

    def test_a_running_watcher_keeps_its_pid_file(self):
        watcher = _start_watcher(self.base / "overlay_status.json")
        self.addCleanup(_stop_process, watcher)

        self.assertFalse(self.watcher.acquire_pid_file(self.pid_path, wait=0.2))
        self.assertEqual(self.pid_path.read_text().strip(), str(watcher.pid))
        self.assertEqual(self.watcher.running_watcher(self.pid_path), watcher.pid)
        # Release by another process leaves it alone.
        self.watcher.release_pid_file(self.pid_path)
        self.assertEqual(self.pid_path.read_text().strip(), str(watcher.pid))

    def test_stale_pid_file_is_replaced(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])  # nosec B603
        dead.wait()
        for stale in (str(dead.pid), "not a pid", str(os.getppid()), ""):
            with self.subTest(pid_file=stale):
                # A finished process, garbage, a live process that is not a
                # watcher, or an empty file: nobody holds the lock.
                self.pid_path.write_text(stale)
                self.assertIsNone(self.watcher.running_watcher(self.pid_path))
                self.assertTrue(self.watcher.acquire_pid_file(self.pid_path))
                self.assertEqual(self.pid_path.read_text().strip(), str(os.getpid()))
                self.watcher.release_pid_file(self.pid_path)

    def test_no_pid_file(self):
        self.assertIsNone(self.watcher.running_watcher(self.pid_path))
        self.assertIsNone(self.watcher.stop_watcher(self.pid_path))


class StatusAndStopOptionTests(SimpleTestCase):
    """--status and --stop, which make overlay and make overlay-stop use."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.output = Path(temp_dir.name) / "tmp" / "overlay_status.json"
        self.pid_path = self.output.parent / "overlay_watcher.pid"

    def option(self, name):
        return subprocess.run(  # nosec B603
            [sys.executable, "-I", str(SCRIPT), name, "--output", str(self.output)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_a_pid_file_naming_another_program_is_not_a_running_helper(self):
        # A PID file left behind by a killed helper whose PID another program
        # has since: here a sleep that must survive --stop.
        other = subprocess.Popen(["sleep", "60"])  # nosec B603 B607
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.pid_path.parent.mkdir(parents=True)
        self.pid_path.write_text(f"{other.pid}\n")

        status = self.option("--status")
        stop = self.option("--stop")

        self.assertEqual((status.returncode, status.stdout), (1, ""))
        self.assertEqual((stop.returncode, stop.stdout), (1, ""))
        self.assertIsNone(other.poll())

    def test_status_and_stop_of_a_running_helper(self):
        watcher = _start_watcher(self.output)
        self.addCleanup(_stop_process, watcher)

        status = self.option("--status")
        self.assertEqual((status.returncode, status.stdout), (0, f"{watcher.pid}\n"))

        stop = self.option("--stop")
        self.assertEqual((stop.returncode, stop.stdout), (0, f"{watcher.pid}\n"))
        # --stop returns once the helper has ended.
        self.assertEqual(watcher.wait(timeout=5), 0)
        self.assertIn("overlay helper stopped", watcher.stdout.read())
        self.assertEqual(self.pid_path.read_text(), "")
        self.assertEqual(self.option("--status").returncode, 1)
        self.assertEqual(self.option("--stop").returncode, 1)

    def test_a_second_helper_exits_while_one_runs(self):
        watcher = _start_watcher(self.output)
        self.addCleanup(_stop_process, watcher)

        second = subprocess.run(  # nosec B603
            [sys.executable, "-I", str(SCRIPT), "--provider", "none"]
            + ["--output", str(self.output)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        self.assertEqual(second.returncode, 0)
        self.assertIn("already running", second.stdout)
        self.assertEqual(self.pid_path.read_text().strip(), str(watcher.pid))


def _fake_program(
    directory, name, *, stdout="", stderr="", code=0, args=None, marker=None
):
    """A shell script that answers one command line; it uses shell builtins only."""
    assert "'" not in stdout and "'" not in stderr
    lines = ["#!/bin/sh"]
    if marker:
        lines.append(f'echo "$*" >> "{marker}"')
    if args:
        condition = " && ".join(
            f'[ "${index}" = "{value}" ]' for index, value in enumerate(args, start=1)
        )
        lines.append(f"if {condition}; then")
    lines.append(f"  printf '%s' '{stdout}'")
    if stderr:
        lines.append(f"  printf '%s' '{stderr}' >&2")
    lines.append(f"  exit {code}")
    if args:
        lines.append("fi")
        lines.append('echo "unexpected arguments: $*" >&2')
        lines.append("exit 64")
    path = Path(directory) / name
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o755)
    return path


class WatcherOnFakeProgramsTests(SimpleTestCase):
    """Runs the script itself with fake netbird/tailscale/ip programs on PATH."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.base = Path(temp_dir.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.output = self.base / "tmp" / "overlay_status.json"
        self.marker = self.base / "calls.log"
        # No interface wt0 unless a test says otherwise; never the host's own ip.
        _fake_program(self.bin, "ip", stderr='Device "wt0" does not exist.\n', code=1)
        settings_override = override_settings(BASE_DIR=str(self.base))
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ("SWARMMEDHUB_OVERLAY", "SWARMMEDHUB_LOCAL_ADDRESSES"):
            os.environ.pop(key, None)
        overlay.reset_warnings()

    def netbird(self, sample=None, *, stdout=None, stderr="", code=0):
        _fake_program(
            self.bin,
            "netbird",
            stdout=_sample(sample) if sample else (stdout or ""),
            stderr=stderr,
            code=code,
            args=["status", "--json"],
            marker=self.marker,
        )

    def tailscale(self, sample):
        _fake_program(
            self.bin,
            "tailscale",
            stdout=_sample(sample),
            args=["status", "--json"],
            marker=self.marker,
        )

    def run_watcher(self, *args, path=None, command=None):
        env = {"PATH": path or str(self.bin)}
        command = command or [sys.executable, "-I", str(SCRIPT)]
        completed = subprocess.run(  # nosec B603
            [*command, "--once", "--output", str(self.output), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(self.output.read_text())

    def test_netbird_daemon_down(self):
        self.netbird(stderr=NETBIRD_NO_DAEMON_STDERR, code=1)

        status = self.run_watcher()

        self.assertEqual(status["provider"], "netbird")
        self.assertEqual(status["state"], "daemon-unavailable")
        self.assertIs(status["connected"], False)
        self.assertIsNone(status["ip"])
        self.assertIn("failed to connect to daemon", status["error"])
        self.assertIs(status["providers"]["tailscale"]["installed"], False)
        hub = overlay.get_overlay_status()
        self.assertEqual(hub.connection, "disconnected")
        self.assertIsNone(hub.current_address)

    def test_netbird_daemon_down_with_interface_address(self):
        self.netbird(stderr=NETBIRD_NO_DAEMON_STDERR, code=1)
        _fake_program(
            self.bin,
            "ip",
            stdout=WT0_LINE,
            args=["-4", "-o", "addr", "show", "dev", "wt0"],
        )

        status = self.run_watcher()

        self.assertEqual(
            (status["ip"], status["state"]), ("100.64.0.10", "interface-only")
        )
        self.assertIsNone(status["connected"])

    def test_netbird_logged_out(self):
        self.netbird("netbird_0.82.1_needs_login.json")

        status = self.run_watcher()

        self.assertEqual(
            (status["provider"], status["state"]), ("netbird", "NeedsLogin")
        )
        self.assertIsNone(status["ip"])
        hub = overlay.get_overlay_status()
        self.assertEqual(hub.summary, "Not connected (NeedsLogin)")
        with self.assertRaisesMessage(
            ValueError, "NetBird is not connected (NeedsLogin)"
        ):
            overlay.resolve_server_address(None)

    def test_netbird_connected(self):
        self.netbird("netbird_connected_synthetic.json")

        status = self.run_watcher()

        self.assertEqual(status["provider"], "netbird")
        self.assertEqual(status["ip"], "100.64.0.10")
        self.assertIs(status["connected"], True)
        self.assertEqual(status["peers"], {"connected": 3, "total": 3})
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o644)
        # The Hub reads what the helper wrote.
        hub = overlay.get_overlay_status()
        self.assertEqual(
            (hub.provider, hub.ip, hub.connection),
            ("netbird", "100.64.0.10", "connected"),
        )
        self.assertEqual(overlay.resolve_server_address(None), "100.64.0.10")
        self.assertEqual(self.marker.read_text().split("\n")[0], "status --json")

    def test_both_installed(self):
        self.netbird("netbird_connected_synthetic.json")
        self.tailscale("tailscale_running_synthetic.json")

        status = self.run_watcher()

        self.assertEqual(status["provider"], "netbird")
        self.assertEqual(status["providers"]["tailscale"]["ip"], "100.64.0.7")
        self.assertEqual(
            overlay.get_local_addresses(), frozenset({"100.64.0.10", "100.64.0.7"})
        )
        os.environ["SWARMMEDHUB_OVERLAY"] = "tailscale"
        self.assertEqual(overlay.get_overlay_ip(), "100.64.0.7")

    def test_both_installed_netbird_logged_out(self):
        self.netbird("netbird_0.82.1_needs_login.json")
        self.tailscale("tailscale_running_synthetic.json")

        status = self.run_watcher()

        self.assertEqual(
            (status["provider"], status["ip"]), ("tailscale", "100.64.0.7")
        )
        self.assertEqual(status["peers"], {"connected": 2, "total": 3})

    def test_provider_from_environment_and_option(self):
        self.netbird("netbird_connected_synthetic.json")
        self.tailscale("tailscale_running_synthetic.json")

        self.assertEqual(
            self.run_watcher("--provider", "tailscale")["provider"], "tailscale"
        )

        completed = subprocess.run(  # nosec B603
            [sys.executable, "-I", str(SCRIPT), "--once", "--output", str(self.output)],
            env={"PATH": str(self.bin), "SWARMMEDHUB_OVERLAY": "tailscale"},
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        self.assertIn("provider=tailscale", completed.stdout)

    def test_provider_none_queries_nothing(self):
        self.netbird("netbird_connected_synthetic.json")

        status = self.run_watcher("--provider", "none")

        self.assertEqual((status["provider"], status["providers"]), ("none", {}))
        self.assertFalse(self.marker.exists())

    def test_old_script_name_runs_the_helper_for_tailscale(self):
        self.netbird("netbird_connected_synthetic.json")
        self.tailscale("tailscale_running_synthetic.json")
        (self.bin / "python3").symlink_to(sys.executable)
        # The wrapper needs sh and dirname; the fakes in bin come first.
        path = f"{self.bin}:/usr/bin:/bin"

        status = self.run_watcher(path=path, command=["/bin/sh", str(WRAPPER)])

        self.assertEqual(
            (status["provider"], status["ip"]), ("tailscale", "100.64.0.7")
        )
        self.assertEqual(status["watcher_mode"], "tailscale")
        # The Hub's default mode still prefers NetBird while it is connected.
        hub = overlay.get_overlay_status()
        self.assertEqual((hub.provider, hub.ip), ("netbird", "100.64.0.10"))
        self.assertEqual(overlay.resolve_server_address(None), "100.64.0.10")
        os.environ["SWARMMEDHUB_OVERLAY"] = "tailscale"
        self.assertEqual(overlay.get_overlay_ip(), "100.64.0.7")

    def test_output_that_is_not_valid_in_the_locales_encoding(self):
        # A management error naming a file whose name is not UTF-8; printf
        # turns \377 into the byte 0xFF.
        script = self.bin / "netbird"
        script.write_text(
            "#!/bin/sh\n"
            "printf '"
            '{"daemonStatus":"Connected","netbirdIp":"100.64.0.10/16",'
            '"management":{"connected":true,'
            '"error":"open /var/lib/netbird/\\377.json"},'
            '"peers":{"connected":1,"total":1}}'
            "'\n"
        )
        script.chmod(0o755)

        status = self.run_watcher()

        self.assertEqual((status["provider"], status["ip"]), ("netbird", "100.64.0.10"))
        self.assertIn("/var/lib/netbird/�.json", status["error"])


class LoopTests(SimpleTestCase):
    """The helper's loop, run in this process with a fake clock."""

    def test_the_loop_survives_errors_and_logs_each_once(self):
        watcher = _load_watcher()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        output = Path(temp_dir.name) / "overlay_status.json"
        failure = RecursionError("maximum recursion depth exceeded")
        results = iter([failure, failure, watcher.build_status("none", {}, 5), failure])

        def collect(mode, interval, env=None):
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result

        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 4:
                raise KeyboardInterrupt

        clock = SimpleNamespace(monotonic=time.monotonic, time=time.time, sleep=sleep)
        signals = SimpleNamespace(signal=lambda *args: None, SIGTERM=signal.SIGTERM)
        log = StringIO()
        with (
            patch.object(watcher, "collect", collect),
            patch.object(watcher, "time", clock),
            patch.object(watcher, "signal", signals),
            redirect_stdout(log),
        ):
            code = watcher.main(["--interval", "5", "--output", str(output)])

        self.assertEqual(code, 0)
        lines = log.getvalue().splitlines()
        errors = [line for line in lines if "RecursionError" in line]
        # Logged when it first occurs, not again while it lasts, and again
        # when it recurs after an update succeeded.
        self.assertEqual(len(errors), 2, lines)
        self.assertIn("provider=none", lines[2])
        self.assertTrue(output.exists())
        self.assertIn("overlay helper stopped", lines[-1])
        self.assertEqual((output.parent / "overlay_watcher.pid").read_text(), "")


def _make_command_lines(target, **overrides):
    """A Makefile target's recipe as the shell commands make would run.

    Expands what the overlay targets use: $(VAR) from simple assignments
    (overridden as with `make VAR=value`), $$, the @ prefix and lines continued
    with a backslash. Each command runs in a shell of its own, as with make.
    """
    text = (REPO_ROOT / "Makefile").read_text()
    values = dict(re.findall(r"(?m)^([A-Z_]+)\s*(?:\?=|:=|=)[ \t]*(.*?)\s*$", text))
    values.update(overrides)
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    commands, current = [], []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        current.append(line[1:])
        if not line.endswith("\\"):
            commands.append("\n".join(current).lstrip("@"))
            current = []

    def expand(match):
        return "$" if match.group(0) == "$$" else values[match.group(1)]

    return [re.sub(r"\$\$|\$\(([A-Z_]+)\)", expand, command) for command in commands]


class MakefileTests(SimpleTestCase):
    """make start runs the overlay helper; make tailscale still works."""

    def test_overlay_targets(self):
        makefile = (REPO_ROOT / "Makefile").read_text()
        self.assertRegex(
            makefile, r"(?m)^start: setup overlay compose-build compose-up "
        )
        self.assertRegex(makefile, r"(?m)^tailscale: overlay ")
        self.assertRegex(makefile, r"(?m)^deinstall: overlay-stop ")
        self.assertIn("scripts/overlay_watcher.py", makefile)


class MakefileRecipeTests(SimpleTestCase):
    """Runs the expanded overlay recipes with /bin/sh in a scratch copy (no make)."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.base = Path(temp_dir.name)
        (self.base / "scripts").mkdir()
        shutil.copy(SCRIPT, self.base / "scripts" / SCRIPT.name)
        self.pid_path = self.base / "tmp" / "overlay_watcher.pid"
        self.watcher = _load_watcher()
        self.addCleanup(self.kill_helper)

    def kill_helper(self):
        pid = self.watcher.running_watcher(self.pid_path)
        if pid:
            os.kill(pid, signal.SIGKILL)

    def recipe(self, target, python=sys.executable):
        output = []
        for command in _make_command_lines(target, OVERLAY_PYTHON=python):
            completed = subprocess.run(  # nosec B603
                ["/bin/sh", "-c", command],
                cwd=self.base,
                env={"PATH": "/usr/bin:/bin", "SWARMMEDHUB_OVERLAY": "none"},
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            output.append(completed.stdout + completed.stderr)
            if completed.returncode:
                return completed.returncode, "".join(output)
        return 0, "".join(output)

    def wait_for_helper(self):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            pid = self.watcher.running_watcher(self.pid_path)
            if pid:
                return pid
            time.sleep(0.1)
        self.fail("the helper did not start")

    def test_a_stale_pid_file_neither_blocks_the_start_nor_stops_another_program(self):
        # The helper was killed, and its PID now belongs to another program.
        other = subprocess.Popen(["sleep", "60"])  # nosec B603 B607
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.pid_path.parent.mkdir()
        self.pid_path.write_text(f"{other.pid}\n")

        self.assertEqual(
            self.recipe("overlay-stop"), (0, "📡 Overlay helper not running\n")
        )
        code, text = self.recipe("overlay")
        self.assertEqual(code, 0)
        self.assertIn("Starting the overlay helper", text)
        helper = self.wait_for_helper()
        self.assertNotEqual(helper, other.pid)

        self.assertEqual(
            self.recipe("overlay"),
            (0, f"📡 Overlay helper already running (PID {helper})\n"),
        )
        self.assertEqual(
            self.recipe("overlay-stop"),
            (0, f"📡 Overlay helper stopped (PID {helper})\n"),
        )
        self.assertIsNone(self.watcher.running_watcher(self.pid_path))
        self.assertEqual(
            self.recipe("overlay-stop"), (0, "📡 Overlay helper not running\n")
        )
        self.assertIsNone(other.poll())

    def test_overlay_stop_fails_only_on_an_error(self):
        # No python3 at all: nothing can be running.
        self.assertEqual(
            self.recipe("overlay-stop", python="/nonexistent/python3")[0], 0
        )
        # --stop exits 3 when it cannot stop the helper.
        self.assertNotEqual(self.recipe("overlay-stop", python="sh -c 'exit 3'")[0], 0)

    def test_overlay_fails_when_the_status_check_fails(self):
        # --status exits 3 on an error (for example an unreadable PID file): the
        # recipe stops instead of starting a helper that cannot take the lock.
        code, text = self.recipe("overlay", python="sh -c 'exit 3'")
        self.assertNotEqual(code, 0)
        self.assertNotIn("Starting the overlay helper", text)


class FreshnessTests(SimpleTestCase):
    def test_timestamps_are_utc(self):
        watcher = _load_watcher()
        stamp = watcher.utc_timestamp(time.time())
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
