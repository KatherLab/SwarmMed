"""Switching a site to a standby FL server (network.failover)."""

from __future__ import annotations

import io
import json
import os
import shutil
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


def _comm_config(internal_host):
    return {
        "allow_adhoc_conns": False,
        "backbone_conn_gen": 2,
        "internal": {
            "scheme": "tcp",
            "resources": {"host": internal_host, "connection_security": "clear"},
        },
    }


def _add_flare_files(prod_dir: Path) -> None:
    """Give the kits of _make_kits the other files NVFlare 2.7.1 provisioning writes."""
    server = prod_dir / "server"
    for name in (
        "rootCA.pem",
        "server.crt",
        "server_host.txt",
        "signature.json",
        "start.sh",
        "stop_fl.sh",
        "sub_start.sh",
    ):
        (server / "startup" / name).write_text("server")
    # The server runs its jobs in its own container, where 'server' is itself.
    _write_json(server / "local" / "comm_config.json", _comm_config("server"))
    for name in (
        "authorization.json.default",
        "log_config.json.default",
        "privacy.json.sample",
        "signature.json",
    ):
        _write_json(server / "local" / name, {"role": "server"})
    for site in ("node-A", "node-B"):
        for name in (
            "client.crt",
            "client.key",
            "rootCA.pem",
            "signature.json",
            "start.sh",
            "stop_fl.sh",
            "sub_start.sh",
        ):
            (prod_dir / site / "startup" / name).write_text(site)
        for name in (
            "authorization.json.default",
            "log_config.json.default",
            "privacy.json.sample",
            "resources.json.default",
        ):
            _write_json(prod_dir / site / "local" / name, {"site": site})
    # A kit provisioned with a client listening address has its own; node-B's
    # kit has none, so FLARE uses its default internal host, localhost.
    _write_json(
        prod_dir / "node-A" / "local" / "comm_config.json", _comm_config("localhost")
    )
    admin = prod_dir / "admin-node-A@nvidia.com" / "startup"
    for name in ("client.crt", "client.key", "rootCA.pem", "fl_admin.sh"):
        (admin / name).write_text("admin")
    _write_json(
        prod_dir / ".client_admin_map.json", {"node-A": "admin-node-A@nvidia.com"}
    )


def _leave_kits_in_site_folder(prod_dir: Path, site: str) -> None:
    """Recreate what an import before the fix left: the copies, and the originals."""
    shutil.copytree(prod_dir / "server" / "startup", prod_dir / site / "server_startup")
    shutil.copytree(prod_dir / "server" / "local", prod_dir / site / "server_local")
    admin = prod_dir / f"admin-{site}@nvidia.com" / "startup"
    if admin.is_dir():
        shutil.copytree(admin, prod_dir / site / "admin_startup")


def _flare_reads(workspace: Path, file_name: str):
    """The file NVFlare 2.7.1 loads for ``file_name`` in a workspace, or None."""
    from nvflare.fuel.utils.config_factory import ConfigFactory

    _format, path = ConfigFactory.search_config_format(file_name, [str(workspace)])
    return Path(path) if path else None


def _site_kits(network) -> dict[str, list[tuple[str, bytes]]]:
    """Each site's kit in the package a Hub exports, as (name, content) pairs."""
    kits = {}
    with zipfile.ZipFile(create_startup_kits_zip(network)) as bundle:
        for name in bundle.namelist():
            if name.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(bundle.read(name))) as kit:
                    kits[name.removesuffix(".zip")] = [
                        (info.filename, kit.read(info)) for info in kit.infolist()
                    ]
    return kits


def _internal_host(workspace: Path) -> str:
    """The host a site's FLARE processes use between parent and job process."""
    from nvflare.fuel.f3.cellnet.connector_manager import ConnectorManager
    from nvflare.fuel.f3.comm_config import CommConfigurator
    from nvflare.fuel.utils.config_service import ConfigService

    ConfigService.reset()
    CommConfigurator.reset()
    try:
        # As FLClientStarterConfiger does in a client's parent and job processes.
        ConfigService.initialize(section_files={}, config_path=[str(workspace)])
        manager = ConnectorManager(
            None, secure=False, comm_configurator=CommConfigurator()
        )
        return manager.get_config_info()["internal_resources"]["host"]
    finally:
        ConfigService.reset()
        CommConfigurator.reset()


class ServerHostTests(SimpleTestCase):
    def test_valid_hosts(self):
        for host in ("192.0.2.10", " 10.0.0.1 "):
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
        _write_json(self.prod_dir / ACTIVE_SERVER_FILE, {"server_host": "192.0.2.10"})

    def test_found_from_every_kit_layout(self):
        for startup in (
            self.prod_dir / "startup",
            self.prod_dir / "node-A" / "startup",
            self.prod_dir / "server" / "startup",
            self.prod_dir / "admin-node-A@nvidia.com" / "startup",
            # The admin kit of a single site's kit imported on its own.
            self.prod_dir / "admin_startup" / "startup",
            # Where earlier imports left a site's admin kit.
            self.prod_dir / "node-A" / "admin_startup" / "startup",
        ):
            with self.subTest(startup=startup):
                self.assertEqual(
                    active_server_host_for_startup(startup), "192.0.2.10"
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
                network_tasks._configured_server_host(startup), "192.0.2.10"
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
        self.assertEqual(host, "192.0.2.10")


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
            self.network, "192.0.2.10", started_server=True, actor=self.user
        )

        self.assertEqual(record["previous_server_host"], "100.64.0.1")
        self.assertTrue(record["server_started_here"])
        stored = json.loads((self.prod_dir / ACTIVE_SERVER_FILE).read_text())
        self.assertEqual(stored["server_host"], "192.0.2.10")

        second = write_active_server(self.network, "100.64.0.1", started_server=False)
        self.assertEqual(second["previous_server_host"], "192.0.2.10")

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
                "192.0.2.10",
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
        self.assertEqual(record["server_host"], "192.0.2.10")

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

    def _start(self, on_docker_run=None, **kwargs):
        commands = []

        def fake_run(command, *args, **run_kwargs):
            commands.append(command)
            if on_docker_run and command[1:2] == ["run"]:
                on_docker_run(command)
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
        write_active_server(self.network, "192.0.2.10", started_server=False)

        runs = self._start(role="client", participants=["node-B"])

        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertIn("swarmmedhub.role=client", run)
        self.assertTrue(run[run.index("--name") + 1].endswith("node-B"))
        self.assertIn("server:192.0.2.10", run)
        self.assertIn("SWARMMEDHUB_SERVER_HOST=192.0.2.10", run)

    def test_unknown_participant_is_an_error(self):
        self._start(role="client", participants=["node-Z"])
        self.assertEqual(self.network.status, "ERROR")

    def test_client_start_first_moves_other_kits_out_of_the_site_workspace(self):
        _add_flare_files(self.prod_dir)
        _leave_kits_in_site_folder(self.prod_dir, "node-B")
        site = self.prod_dir / "node-B"
        self.assertEqual(_internal_host(site), "server")
        left_at_launch = []

        def at_launch(_command):
            left_at_launch.extend(
                folder
                for folder in ("server_startup", "server_local", "admin_startup")
                if (site / folder).exists()
            )

        runs = self._start(
            on_docker_run=at_launch, role="client", participants=["node-B"]
        )

        self.assertEqual(len(runs), 1)
        self.assertEqual(left_at_launch, [])
        self.assertEqual(_internal_host(site), "localhost")
        # The server's own settings are left as they are.
        self.assertEqual(
            json.loads((self.prod_dir / "server/local/comm_config.json").read_text()),
            _comm_config("server"),
        )

    def test_client_start_moves_other_kits_before_fixing_its_comm_config(self):
        _add_flare_files(self.prod_dir)
        _leave_kits_in_site_folder(self.prod_dir, "node-B")
        # The server's local folder has no copy outside node-B's workspace, so
        # starting the client moves it there; until then FLARE reads it.
        shutil.rmtree(self.prod_dir / "server" / "local")
        site = self.prod_dir / "node-B"
        self.assertEqual(
            _flare_reads(site, "comm_config.json"),
            site / "server_local/comm_config.json",
        )

        runs = self._start(role="client", participants=["node-B"])

        self.assertEqual(len(runs), 1)
        self.assertFalse((site / "server_local").exists())
        self.assertEqual(_internal_host(site), "localhost")
        # Only the client's settings are made portable: the server's are moved
        # as they were.
        self.assertEqual(
            json.loads((self.prod_dir / "server/local/comm_config.json").read_text()),
            _comm_config("server"),
        )


class BundleTests(NetworkFixture, TestCase):
    def setUp(self):
        super().setUp()
        _add_flare_files(self.prod_dir)

    def _import(self, package) -> Path:
        """Import a package into a Hub that has none of the network's files."""
        identifier = self.network.identifier
        SwarmNetwork.objects.filter(pk=self.network.pk).delete()
        # Otherwise the provisioned kits would still be in the network's folder.
        shutil.rmtree(network_prod_dir(self.network).parents[2])
        package.seek(0)
        imported = import_network(
            project=self.project,
            actor=self.user,
            name="Imported",
            package_source=package,
        )
        self.assertEqual(str(imported.identifier), str(identifier))
        return network_prod_dir(imported)

    def test_bundle_carries_the_server_local_folder_and_import_restores_it(self):
        buffer = create_startup_kits_zip(self.network)
        with zipfile.ZipFile(buffer) as bundle:
            kit = zipfile.ZipFile(io.BytesIO(bundle.read("node-A.zip")))
            names = kit.namelist()
        self.assertIn("server_local/resources.json.default", names)
        self.assertIn("server_startup/fed_server.json", names)
        self.assertIn("admin_startup/fed_admin.json", names)

        prod_dir = self._import(buffer)

        restored = prod_dir / "server" / "local" / "resources.json.default"
        self.assertEqual(json.loads(restored.read_text()), {"format_version": 2})
        self.assertTrue((prod_dir / "server/local/comm_config.json").is_file())
        self.assertTrue((prod_dir / "server/startup/fed_server.json").is_file())
        self.assertTrue(
            (prod_dir / "admin-node-A@nvidia.com/startup/fed_admin.json").is_file()
        )
        # A site's folder is its FLARE workspace: no other role's kit stays there.
        for site in ("node-A", "node-B"):
            self.assertTrue((prod_dir / site / "startup/fed_client.json").is_file())
            for folder in ("server_startup", "server_local", "admin_startup"):
                self.assertFalse((prod_dir / site / folder).exists(), folder)

    def test_after_import_each_site_reads_its_own_settings(self):
        prod_dir = self._import(create_startup_kits_zip(self.network))
        node_a = prod_dir / "node-A"
        node_b = prod_dir / "node-B"
        server = prod_dir / "server"
        # A site's workspace also holds the folders of the jobs it has run.
        _write_json(node_a / "job-1/app_node-A/config/config_fed_client.json", {})

        for workspace, own_comm_config in (
            (node_a, node_a / "local/comm_config.json"),
            (node_b, None),
            (server, server / "local/comm_config.json"),
        ):
            with self.subTest(workspace=workspace.name):
                self.assertEqual(
                    _flare_reads(workspace, "comm_config.json"), own_comm_config
                )
                # FLARE asks for the name local/ has, and finds it anywhere.
                self.assertEqual(
                    _flare_reads(workspace, "resources.json.default"),
                    workspace / "local/resources.json.default",
                )
        self.assertEqual(_internal_host(node_a), "localhost")
        self.assertEqual(_internal_host(node_b), "localhost")
        self.assertEqual(_internal_host(server), "server")

    def test_a_hub_that_imported_the_package_exports_the_same_kits(self):
        prod_dir = self._import(create_startup_kits_zip(self.network))
        imported = SwarmNetwork.objects.get(identifier=self.network.identifier)
        # The provisioning Hub's list of admin kits is not in the package.
        self.assertFalse((prod_dir / ".client_admin_map.json").exists())

        with zipfile.ZipFile(create_startup_kits_zip(imported)) as bundle:
            kit = zipfile.ZipFile(io.BytesIO(bundle.read("node-A.zip")))
            names = kit.namelist()

        self.assertEqual(names.count("admin_startup/fed_admin.json"), 1)
        self.assertEqual(names.count("server_startup/fed_server.json"), 1)
        self.assertEqual(names.count("server_local/comm_config.json"), 1)

    def test_a_hub_that_imported_the_package_exports_each_file_once(self):
        self.network.gossip_token = "network-token"
        self.network.save(update_fields=["gossip_token"])
        prod_dir = self._import(create_startup_kits_zip(self.network))
        imported = SwarmNetwork.objects.get(identifier=self.network.identifier)
        expected = _site_kits(imported)
        # As an import before the fix left each site's folder, if no client has
        # started since: the server's and the admin's kits are in it as well.
        for site in ("node-A", "node-B"):
            _leave_kits_in_site_folder(prod_dir, site)
        # The copies can differ: an import writes the server's address it
        # resolved into the Hub's copy, not into the one it left there.
        (prod_dir / "server/startup/server_host.txt").write_text("resolved")

        kits = _site_kits(imported)

        self.assertEqual(sorted(kits), ["node-A", "node-B"])
        for site, files in kits.items():
            names = [name for name, _content in files]
            with self.subTest(site=site):
                duplicates = sorted({name for name in names if names.count(name) > 1})
                self.assertEqual(duplicates, [])
                self.assertEqual(
                    sorted(names), sorted(name for name, _content in expected[site])
                )
                self.assertIn((".gossip_token", b"network-token"), files)
                self.assertIn(("server_startup/server_host.txt", b"resolved"), files)

    def test_a_single_sites_kit_keeps_the_server_kit_outside_its_workspace(self):
        with zipfile.ZipFile(create_startup_kits_zip(self.network)) as bundle:
            site_kit = io.BytesIO(bundle.read("node-A.zip"))

        # prod_00 itself is the site's FLARE workspace here.
        prod_dir = self._import(site_kit)
        network_dir = prod_dir.parents[2]
        self.assertFalse((prod_dir / "server_startup").exists())
        self.assertFalse((prod_dir / "server_local").exists())
        self.assertTrue((network_dir / "server_startup/fed_server.json").is_file())
        self.assertTrue((network_dir / "server_local/comm_config.json").is_file())
        self.assertTrue((prod_dir / "admin_startup/startup/fed_admin.json").is_file())
        self.assertEqual(
            _flare_reads(prod_dir, "comm_config.json"),
            prod_dir / "local/comm_config.json",
        )
        self.assertEqual(
            _flare_reads(prod_dir, "resources.json.default"),
            prod_dir / "local/resources.json.default",
        )


class LegacyImportRepairTests(SimpleTestCase):
    """Starting a client clears what an import before the fix left in its workspace."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.network_dir = Path(temp_dir.name) / "network"
        self.prod_dir = self.network_dir / "workspace" / "demo" / "prod_00"
        _make_kits(self.prod_dir)
        _add_flare_files(self.prod_dir)
        self.logger = MagicMock()

    def _repair(self, site):
        return network_tasks._move_foreign_kit_folders(
            str(self.prod_dir / site), str(self.prod_dir), site, logger=self.logger
        )

    @staticmethod
    def _files(folder: Path) -> dict:
        return {
            str(path.relative_to(folder)): path.read_bytes()
            for path in sorted(folder.rglob("*"))
            if path.is_file()
        }

    def test_folders_whose_copies_exist_are_removed(self):
        _leave_kits_in_site_folder(self.prod_dir, "node-A")
        _leave_kits_in_site_folder(self.prod_dir, "node-B")
        server_kit = self._files(self.prod_dir / "server")
        node_b = self.prod_dir / "node-B"
        # node-B has no comm_config.json of its own, so FLARE reads the server's.
        self.assertEqual(
            _flare_reads(node_b, "comm_config.json"),
            node_b / "server_local/comm_config.json",
        )
        self.assertEqual(_internal_host(node_b), "server")

        self.assertEqual(len(self._repair("node-A")), 3)
        self.assertEqual(len(self._repair("node-B")), 2)

        for site in ("node-A", "node-B"):
            for folder in ("server_startup", "server_local", "admin_startup"):
                self.assertFalse((self.prod_dir / site / folder).exists())
        self.assertEqual(self._files(self.prod_dir / "server"), server_kit)
        self.assertTrue(
            (self.prod_dir / "admin-node-A@nvidia.com/startup/fed_admin.json").is_file()
        )
        self.assertEqual(_internal_host(node_b), "localhost")
        self.assertEqual(
            _flare_reads(node_b, "resources.json.default"),
            node_b / "local/resources.json.default",
        )
        node_a = self.prod_dir / "node-A"
        self.assertEqual(
            _flare_reads(node_a, "comm_config.json"), node_a / "local/comm_config.json"
        )
        self.assertIn("Removed", self.logger.network.info.call_args_list[0].args[0])
        # Nothing is left to do the next time the client starts.
        self.assertEqual(self._repair("node-A"), [])

    def test_the_copy_in_use_wins_and_gets_the_files_it_lacks(self):
        _leave_kits_in_site_folder(self.prod_dir, "node-A")
        startup = self.prod_dir / "server" / "startup"
        (startup / "server_host.txt").write_text("standby")
        (self.prod_dir / "server/local/privacy.json.sample").unlink()

        self._repair("node-A")

        self.assertEqual((startup / "server_host.txt").read_text(), "standby")
        self.assertEqual(
            json.loads(
                (self.prod_dir / "server/local/privacy.json.sample").read_text()
            ),
            {"role": "server"},
        )
        self.assertFalse((self.prod_dir / "node-A/server_local").exists())

    def test_a_folder_without_copy_is_moved_not_deleted(self):
        _leave_kits_in_site_folder(self.prod_dir, "node-B")
        server_local = self._files(self.prod_dir / "server" / "local")
        # As after an import by a release that did not copy server_local.
        shutil.rmtree(self.prod_dir / "server" / "local")

        done = self._repair("node-B")

        self.assertEqual(self._files(self.prod_dir / "server" / "local"), server_local)
        self.assertFalse((self.prod_dir / "node-B/server_local").exists())
        self.assertTrue(any(line.startswith("Moved") for line in done))

    def test_a_folder_that_cannot_be_moved_stays(self):
        _leave_kits_in_site_folder(self.prod_dir, "node-B")

        with patch("shutil.copytree", side_effect=OSError("No space left on device")):
            done = self._repair("node-B")

        self.assertEqual(done, [])
        left = self.prod_dir / "node-B" / "server_local"
        self.assertTrue((left / "comm_config.json").exists())
        self.assertEqual(self.logger.network.warning.call_count, 2)

    def test_a_single_sites_kit_moves_the_server_kit_to_the_network_folder(self):
        # A single site's kit imported on its own: prod_00 is the site's workspace.
        network_dir = self.network_dir.parent / "single-kit"
        workspace = network_dir / "workspace" / "demo" / "prod_00"
        shutil.copytree(self.prod_dir / "node-B", workspace)
        shutil.copytree(self.prod_dir / "server/startup", workspace / "server_startup")
        shutil.copytree(self.prod_dir / "server/local", workspace / "server_local")
        admin = workspace / "admin_startup" / "startup"
        shutil.copytree(self.prod_dir / "admin-node-A@nvidia.com/startup", admin)
        self.assertEqual(_internal_host(workspace), "server")

        done = network_tasks._move_foreign_kit_folders(
            str(workspace), str(workspace), "node-B", logger=self.logger
        )

        self.assertEqual(len(done), 2)
        self.assertFalse((workspace / "server_local").exists())
        self.assertTrue((network_dir / "server_local/comm_config.json").is_file())
        self.assertTrue((network_dir / "server_startup/fed_server.json").is_file())
        # The Hub's admin kit for the network stays where the Hub uses it.
        self.assertTrue((admin / "fed_admin.json").is_file())
        self.assertEqual(_internal_host(workspace), "localhost")


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
                "192.0.2.10",
                "--start-server",
                "--wait",
            ]
        )
        self.assertEqual(
            (args.handler, args.server_host, args.start_server, args.wait),
            ("network_failover", "192.0.2.10", True, True),
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
