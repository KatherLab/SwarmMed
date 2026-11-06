"""Switching a site to a standby FL server (network.failover)."""

from __future__ import annotations

import io
import json
import os
import subprocess  # nosec B404
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from swarmed_cli.main import build_parser

from project.models import Project

from . import tasks as network_tasks
from .failover import (
    ACTIVE_SERVER_FILE,
    active_server_host_for_startup,
    ensure_server_kit_complete,
    is_valid_server_host,
    network_prod_dir,
    write_active_server,
)
from .models import SwarmNetwork, SwarmParticipant
from .services import import_network
from .utils import create_startup_kits_zip


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _make_kits(prod_dir: Path, clients=("node-A", "node-B"), server_host="100.64.0.1"):
    """A provisioned prod_00 folder with a server, client and admin kits."""
    _write_json(
        prod_dir / "server" / "startup" / "fed_server.json",
        {"servers": [{"service": {"target": "server:8002"}, "admin_port": 8003}]},
    )
    (prod_dir / "server" / "startup" / "server.key").write_text("not a real key")
    _write_json(
        prod_dir / "server" / "local" / "resources.json.default", {"format_version": 2}
    )
    for client in clients:
        _write_json(prod_dir / client / "startup" / "fed_client.json", {"servers": []})
        (prod_dir / client / "startup" / "server_host.txt").write_text(server_host)
        (prod_dir / client / "local").mkdir(parents=True, exist_ok=True)
    admin = prod_dir / "admin-node-A@nvidia.com" / "startup"
    _write_json(admin / "fed_admin.json", {"admin": {}})
    (admin / "server_host.txt").write_text(server_host)


class ServerHostTests(SimpleTestCase):
    def test_valid_hosts(self):
        for host in ("100.64.4.103", " 10.0.0.1 "):
            with self.subTest(host=host):
                self.assertTrue(is_valid_server_host(host))
        # Docker's --add-host takes only an IP address, so host names are refused.
        for host in (
            "",
            "300.1.1.1",
            "1.2.3",
            "bad host",
            "standby.example.org",
            "site-b",
        ):
            with self.subTest(host=host):
                self.assertFalse(is_valid_server_host(host))


class ActiveServerFileTests(SimpleTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.prod_dir = Path(temp_dir.name) / "workspace" / "demo" / "prod_00"
        _make_kits(self.prod_dir)
        _write_json(self.prod_dir / ACTIVE_SERVER_FILE, {"server_host": "100.64.4.103"})

    def test_found_from_every_kit_layout(self):
        for startup in (
            self.prod_dir / "startup",
            self.prod_dir / "node-A" / "startup",
            self.prod_dir / "node-A" / "admin_startup" / "startup",
        ):
            with self.subTest(startup=startup):
                self.assertEqual(
                    active_server_host_for_startup(startup), "100.64.4.103"
                )

    def test_ignored_outside_a_provisioning_folder(self):
        other = self.prod_dir.parent / "elsewhere" / "startup"
        _write_json(
            self.prod_dir.parent / ACTIVE_SERVER_FILE, {"server_host": "1.1.1.1"}
        )
        self.assertEqual(active_server_host_for_startup(other), "")

    def test_switch_wins_over_environment_and_kit_file(self):
        startup = str(self.prod_dir / "node-A" / "startup")
        with patch.dict(os.environ, {"SWARMMEDHUB_SERVER_HOST": "100.64.0.7"}):
            self.assertEqual(
                network_tasks._configured_server_host(startup), "100.64.4.103"
            )
        (self.prod_dir / ACTIVE_SERVER_FILE).unlink()
        with patch.dict(os.environ, {"SWARMMEDHUB_SERVER_HOST": "100.64.0.7"}):
            self.assertEqual(
                network_tasks._configured_server_host(startup), "100.64.0.7"
            )

    def test_admin_session_targets_the_switched_server(self):
        from training.runtime import resolve_admin_session_target

        network = SimpleNamespace(
            admin_startup_dir=str(
                self.prod_dir / "admin-node-A@nvidia.com" / "startup"
            ),
            project=SimpleNamespace(title="Demo", identifier="p"),
            identifier="n",
        )
        with patch.dict(os.environ, {"SWARMMEDHUB_SERVER_HOST": "100.64.0.7"}):
            _name, _session_dir, host = resolve_admin_session_target(network)
        self.assertEqual(host, "100.64.4.103")


class ServerKitTests(SimpleTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.prod_dir = Path(temp_dir.name) / "prod_00"

    def test_a_bundle_without_server_kit_cannot_host_the_server(self):
        self.prod_dir.mkdir()
        with self.assertRaisesRegex(FileNotFoundError, "no FL server kit"):
            ensure_server_kit_complete(self.prod_dir)

    def test_missing_local_settings_are_written(self):
        _write_json(self.prod_dir / "server" / "startup" / "fed_server.json", {})

        written = ensure_server_kit_complete(self.prod_dir)

        local = self.prod_dir / "server" / "local"
        resources = json.loads((local / "resources.json.default").read_text())
        self.assertEqual(
            [c["id"] for c in resources["components"]],
            [
                "job_scheduler",
                "job_manager",
                "job_store",
                "process_launcher",
                "log_receiver",
            ],
        )
        self.assertTrue((local / "authorization.json.default").read_text())
        self.assertIn(str(local / "comm_config.json"), written)
        # FLARE's server does not start without a logging configuration.
        from nvflare.apis.workspace import Workspace

        log_config = Workspace(str(self.prod_dir / "server")).get_log_config_file_path()
        self.assertEqual(log_config, str(local / "log_config.json.default"))
        self.assertIn("handlers", json.loads(Path(log_config).read_text()))

    def test_only_missing_files_are_written(self):
        _make_kits(self.prod_dir)

        written = ensure_server_kit_complete(self.prod_dir)

        local = self.prod_dir / "server" / "local"
        self.assertNotIn(str(local / "resources.json.default"), written)
        self.assertIn(str(local / "log_config.json.default"), written)

    def test_existing_local_settings_are_kept(self):
        _make_kits(self.prod_dir)
        ensure_server_kit_complete(self.prod_dir)
        self.assertEqual(ensure_server_kit_complete(self.prod_dir), [])
        self.assertEqual(
            json.loads(
                (self.prod_dir / "server/local/resources.json.default").read_text()
            ),
            {"format_version": 2},
        )


class NetworkFixture:
    def setUp(self):
        super().setUp()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        settings_override = override_settings(BASE_DIR=Path(temp_dir.name))
        settings_override.enable()
        self.addCleanup(settings_override.disable)
        self.user = User.objects.create_user(
            username="operator", password="test-password"
        )  # nosec B106
        self.project = Project.objects.create(title="Demo Study", author=self.user)
        self.network = SwarmNetwork.objects.create(
            name="Demo", project=self.project, author=self.user, status="RUNNING"
        )
        for participant_id, role in (
            ("server", "SERVER"),
            ("node-A", "CLIENT"),
            ("node-B", "CLIENT"),
        ):
            SwarmParticipant.objects.create(
                network=self.network,
                user=self.user,
                role=role,
                participant_id=participant_id,
            )
        self.prod_dir = network_prod_dir(self.network)
        _make_kits(self.prod_dir)


class WriteActiveServerTests(NetworkFixture, TestCase):
    def test_record_names_the_previous_server(self):
        record = write_active_server(
            self.network, "100.64.4.103", started_server=True, actor=self.user
        )

        self.assertEqual(record["previous_server_host"], "100.64.0.1")
        self.assertTrue(record["server_started_here"])
        stored = json.loads((self.prod_dir / ACTIVE_SERVER_FILE).read_text())
        self.assertEqual(stored["server_host"], "100.64.4.103")

        second = write_active_server(self.network, "100.64.0.1", started_server=False)
        self.assertEqual(second["previous_server_host"], "100.64.4.103")

    def test_rejects_a_bad_address(self):
        with self.assertRaises(ValueError):
            write_active_server(self.network, "not a host", started_server=False)


class FailoverTaskTests(NetworkFixture, TestCase):
    def _run(
        self,
        *,
        start_server,
        clients=("node-A",),
        servers=(),
        start_status="RUNNING",
        previous_status="RUNNING",
    ):
        calls = []
        self.docker_commands = []
        self.status_at_start = []

        def fake_start(network_id, user_id, role=None, participants=None):
            calls.append((role, participants))
            self.status_at_start.append(
                SwarmNetwork.objects.get(identifier=network_id).status
            )
            SwarmNetwork.objects.filter(identifier=network_id).update(
                status=start_status
            )

        def fake_participants(_network_id, role, *_args):
            return list(clients if role == "client" else servers)

        def fake_docker(command, *_args, **_kwargs):
            self.docker_commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with (
            patch.object(
                network_tasks, "start_swarm_network_task", side_effect=fake_start
            ),
            patch.object(
                network_tasks,
                "_local_runtime_participants",
                side_effect=fake_participants,
            ),
            patch.object(network_tasks.subprocess, "run", side_effect=fake_docker),
        ):
            network_tasks.failover_network_task(
                str(self.network.identifier),
                self.user.id,
                "100.64.4.103",
                start_server,
                previous_status,
            )
        self.network.refresh_from_db()
        return calls

    def test_switching_away_stops_this_hubs_own_server(self):
        self._run(start_server=False, servers=("server",))

        self.assertEqual(len(self.docker_commands), 1)
        self.assertEqual(self.docker_commands[0][1:4], ["stop", "-t", "15"])
        self.assertTrue(self.docker_commands[0][-1].endswith("-server"))

    def test_running_jobs_are_marked_failed(self):
        from training.models import TrainingJob

        running = TrainingJob.objects.create(
            project=self.project, network=self.network, status="RUNNING"
        )
        finished = TrainingJob.objects.create(
            project=self.project, network=self.network, status="COMPLETED"
        )

        self._run(start_server=False)

        running.refresh_from_db()
        finished.refresh_from_db()
        self.assertEqual((running.status, finished.status), ("FAILED", "COMPLETED"))
        self.assertIsNotNone(running.completed_at)

    def test_standby_starts_the_server_then_restarts_its_clients(self):
        calls = self._run(start_server=True)

        self.assertEqual(calls, [("server", None), ("client", ["node-A"])])
        # Not RUNNING between the server start and the client restart, so that
        # 'network failover --wait' waits for the clients.
        self.assertEqual(self.status_at_start[1], "STARTING")
        self.assertEqual(self.network.status, "RUNNING")
        record = json.loads((self.prod_dir / ACTIVE_SERVER_FILE).read_text())
        self.assertEqual(record["server_host"], "100.64.4.103")

    def test_other_sites_only_restart_their_clients(self):
        self.assertEqual(self._run(start_server=False), [("client", ["node-A"])])

    def test_hub_without_clients_only_switches_the_admin_connection(self):
        self.assertEqual(self._run(start_server=False, clients=()), [])
        self.assertEqual(self.network.status, "RUNNING")

    def test_hub_without_clients_keeps_a_stopped_network_stopped(self):
        self._run(start_server=False, clients=(), previous_status="STOPPED")
        self.assertEqual(self.network.status, "STOPPED")
        self.assertTrue((self.prod_dir / ACTIVE_SERVER_FILE).exists())

    def test_failed_server_start_stops_the_switch(self):
        calls = self._run(start_server=True, start_status="ERROR")

        self.assertEqual(calls, [("server", None)])
        self.assertEqual(self.network.status, "ERROR")
        # The Hub keeps using the server it used before.
        self.assertFalse((self.prod_dir / ACTIVE_SERVER_FILE).exists())

    def test_failed_server_start_restores_an_earlier_switch(self):
        earlier = {"server_host": "100.64.0.9", "server_started_here": False}
        _write_json(self.prod_dir / ACTIVE_SERVER_FILE, earlier)

        self._run(start_server=True, start_status="ERROR")

        self.assertEqual(
            json.loads((self.prod_dir / ACTIVE_SERVER_FILE).read_text()), earlier
        )

    def test_docker_failure_is_not_taken_for_no_containers(self):
        failed = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="permission denied"
        )
        with (
            patch.object(network_tasks.subprocess, "run", return_value=failed),
            self.assertRaisesRegex(RuntimeError, "permission denied"),
        ):
            network_tasks._local_runtime_participants(
                str(self.network.identifier), "server", "docker", {}, str(self.prod_dir)
            )

    def test_service_rejects_a_bad_address_before_queueing(self):
        from .services import failover_network

        with (
            patch.object(network_tasks.failover_network_task, "delay") as delay,
            self.assertRaises(ValueError),
        ):
            failover_network(self.network, self.user, server_host="no such host!")
        delay.assert_not_called()


class StartRoleTests(NetworkFixture, TestCase):
    """start_swarm_network_task starts only what the call asks for."""

    def _start(self, **kwargs):
        commands = []

        def fake_run(command, *args, **run_kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        environment = {
            "SWARMMEDHUB_FLARE_IMAGE": "runtime:test",
            "SWARMMEDHUB_ENABLE_GPU": "false",
            "SWARMMEDHUB_CLIENT_HOST_NETWORK": "true",
        }
        with (
            patch.dict(os.environ, environment),
            patch.multiple(
                network_tasks,
                _ensure_runtime_requirements_file=MagicMock(),
                _has_custom_runtime_requirements=MagicMock(return_value=False),
                _ensure_docker_network=MagicMock(),
                _resolve_bind_source_path=MagicMock(side_effect=lambda path: path),
                _add_required_port_mapping=MagicMock(),
                _is_container_running=MagicMock(return_value=True),
                _connect_control_containers_to_network=MagicMock(),
                _count_running_labeled_containers=MagicMock(return_value=1),
                run_nvflare_preflight_check=MagicMock(),
            ),
            patch.object(network_tasks.docker, "from_env", side_effect=RuntimeError),
            patch.object(network_tasks.subprocess, "run", side_effect=fake_run),
            patch.object(network_tasks.time, "sleep"),
        ):
            network_tasks.start_swarm_network_task(
                str(self.network.identifier), self.user.id, **kwargs
            )
        self.network.refresh_from_db()
        return [c for c in commands if c[1:2] == ["run"]]

    def test_server_role_starts_only_the_server(self):
        runs = self._start(role="server")

        self.assertEqual(self.network.status, "RUNNING")
        self.assertEqual(len(runs), 1)
        self.assertIn("swarmmedhub.role=server", runs[0])

    def test_client_role_with_participants_uses_the_switched_server(self):
        write_active_server(self.network, "100.64.4.103", started_server=False)

        runs = self._start(role="client", participants=["node-B"])

        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertIn("swarmmedhub.role=client", run)
        self.assertTrue(run[run.index("--name") + 1].endswith("node-B"))
        self.assertIn("server:100.64.4.103", run)
        self.assertIn("SWARMMEDHUB_SERVER_HOST=100.64.4.103", run)

    def test_unknown_participant_is_an_error(self):
        self._start(role="client", participants=["node-Z"])
        self.assertEqual(self.network.status, "ERROR")


class BundleTests(NetworkFixture, TestCase):
    def test_bundle_carries_the_server_local_folder_and_import_restores_it(self):
        buffer = create_startup_kits_zip(self.network)
        with zipfile.ZipFile(buffer) as bundle:
            kit = zipfile.ZipFile(io.BytesIO(bundle.read("node-A.zip")))
            names = kit.namelist()
        self.assertIn("server_local/resources.json.default", names)
        self.assertIn("server_startup/fed_server.json", names)

        identifier = self.network.identifier
        SwarmNetwork.objects.filter(pk=self.network.pk).delete()
        buffer.seek(0)
        imported = import_network(
            project=self.project,
            actor=self.user,
            name="Imported",
            package_source=buffer,
        )

        self.assertEqual(str(imported.identifier), str(identifier))
        restored = (
            network_prod_dir(imported) / "server" / "local" / "resources.json.default"
        )
        self.assertEqual(json.loads(restored.read_text()), {"format_version": 2})


class StopTaskTests(SimpleTestCase):
    def test_stop_is_a_celery_task_again(self):
        self.assertTrue(hasattr(network_tasks.stop_swarm_network_task, "delay"))
        self.assertFalse(hasattr(network_tasks._stop_unfinished_training_jobs, "delay"))


class CLIParserTests(SimpleTestCase):
    def test_failover_and_resume_commands(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "network",
                "failover",
                "net-1",
                "--server-host",
                "100.64.4.103",
                "--start-server",
                "--wait",
            ]
        )
        self.assertEqual(
            (args.handler, args.server_host, args.start_server, args.wait),
            ("network_failover", "100.64.4.103", True, True),
        )
        args = parser.parse_args(
            [
                "training",
                "resume",
                "job-1",
                "--start-round",
                "8",
                "--rounds",
                "20",
                "--force",
            ]
        )
        self.assertEqual(
            (
                args.handler,
                args.job_identifier,
                args.start_round,
                args.rounds,
                args.force,
            ),
            ("training_resume", "job-1", 8, 20, True),
        )
        args = parser.parse_args(["training", "checkpoints", "--network", "net-1"])
        self.assertEqual(args.handler, "training_checkpoints")
