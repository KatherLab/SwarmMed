"""A default installation: the Celery worker runs as appuser, the CLI as root.

The worker cannot enter every folder on the Docker host's path to the checkout,
such as /root, and cannot change what the CLI wrote as root unless the CLI
handed it over.
"""

from __future__ import annotations

import errno
import io
import json
import os
import shutil
import subprocess  # nosec B404
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from . import services as network_services
from . import tasks as network_tasks
from . import utils as network_utils
from .failover import (
    ACTIVE_SERVER_FILE,
    FLARE_ADMIN_TIMEOUT_ENV,
    apply_flare_admin_timeout,
    network_prod_dir,
)
from .models import SwarmNetwork
from .test_failover import NetworkFixture, _add_flare_files, _internal_host
from .utils import create_startup_kits_zip

# A folder on the Docker host's path to the checkout that the worker cannot
# search, as /root is for appuser. It does not exist here: _cannot_enter fakes it.
HOST_PREFIX = "/root-of-docker-host/"


def _under_host_prefix(path):
    """The Docker host's path of a folder of the Hub, as HOST_PROJECT_PATH gives it."""
    return HOST_PREFIX + "SwarmMed" + str(path)


@contextmanager
def _cannot_enter(prefix):
    """os.stat as for a user who cannot search ``prefix``.

    pathlib and os.path both go through os.stat, so this reproduces the
    worker's EACCES whatever user runs the tests, root included.
    """
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        name = "" if isinstance(path, int) else os.fsdecode(path)
        if name.startswith(prefix):
            raise PermissionError(errno.EACCES, "Permission denied", name)
        return real_stat(path, *args, **kwargs)

    with patch.object(os, "stat", stat):
        yield


# The worker's uid and gid in the tests; appuser's are 1000 in the image.
WORKER_IDS = (4242, 4242)


@contextmanager
def _as_root():
    """Act as the documented CLI does: as root in the app container.

    Yields:
        MagicMock: Records os.chown instead of changing any owner.
    """
    with (
        patch.object(os, "geteuid", return_value=0),
        patch.object(os, "chown") as chown,
        patch.object(
            network_utils, "_runtime_service_identity", return_value=WORKER_IDS
        ),
    ):
        yield chown


def _handed_over(chown) -> list[Path]:
    """The paths that a chown recorder saw handed to the worker, in call order."""
    return [
        Path(call.args[0])
        for call in chown.call_args_list
        if call.args[1:] == WORKER_IDS and call.kwargs == {"follow_symlinks": False}
    ]


def _tree(folder: Path) -> set[Path]:
    """A folder and everything below it."""
    paths = {folder}
    for root, dirs, files in os.walk(folder):
        paths.update(Path(root) / name for name in dirs + files)
    return paths


def _not_handed_over(chown, folder: Path) -> list[str]:
    """What of ``folder`` and below a chown recorder did not see handed over."""
    handed = set(_handed_over(chown))
    return sorted(
        str(path.relative_to(folder.parent))
        for path in _tree(folder)
        if path not in handed
    )


class RuntimeFixture(NetworkFixture):
    """Runs the start and failover tasks with Docker faked, as StartRoleTests does."""

    @contextmanager
    def _docker(self, on_docker_run=None, bind_source=None, image="runtime:test"):
        """Yield the list that receives each `docker run` command.

        Args:
            on_docker_run: Called with each `docker run` command.
            bind_source: Replaces _resolve_bind_source_path.
            image: SWARMMEDHUB_FLARE_IMAGE; None builds the default runtime image.
        """
        runs = []

        def fake_run(command, *args, **kwargs):
            if command[1:2] == ["run"]:
                runs.append(command)
                if on_docker_run:
                    on_docker_run(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        environment = {
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
                _resolve_bind_source_path=MagicMock(
                    side_effect=bind_source or (lambda path: path)
                ),
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
            if image:
                os.environ["SWARMMEDHUB_FLARE_IMAGE"] = image
            else:
                os.environ.pop("SWARMMEDHUB_FLARE_IMAGE", None)
            yield runs

    def _start(self, *, on_docker_run=None, bind_source=None, image="runtime:test", **kwargs):
        with self._docker(on_docker_run, bind_source, image) as runs:
            network_tasks.start_swarm_network_task(
                str(self.network.identifier), self.user.id, **kwargs
            )
        self.network.refresh_from_db()
        return runs

    def _failover(
        self,
        *,
        start_server,
        server_host="192.0.2.10",
        clients=("node-B",),
        servers=(),
        bind_source=None,
        image="runtime:test",
    ):
        """Run failover_network_task with the real start task.

        ``clients`` and ``servers`` are the network's containers at this Hub.
        """

        def fake_participants(_network_id, role, *_args):
            return list(clients if role == "client" else servers)

        with (
            self._docker(bind_source=bind_source, image=image) as runs,
            patch.object(
                network_tasks,
                "_local_runtime_participants",
                side_effect=fake_participants,
            ),
        ):
            network_tasks.failover_network_task(
                str(self.network.identifier),
                self.user.id,
                server_host,
                start_server,
                "RUNNING",
            )
        self.network.refresh_from_db()
        return runs


class HostPathTests(RuntimeFixture, TestCase):
    """The Docker host's path to the checkout lies under a folder the worker cannot enter."""

    def test_admin_timeout_skips_a_host_path_the_worker_cannot_enter(self):
        server = self.prod_dir / "server"
        log = MagicMock()

        with (
            patch.dict(os.environ, {FLARE_ADMIN_TIMEOUT_ENV: "300"}),
            _cannot_enter(HOST_PREFIX),
        ):
            written = apply_flare_admin_timeout(
                [server, HOST_PREFIX + "SwarmMed/prod_00/server"], logger=log
            )

        resources = server / "local" / "resources.json"
        self.assertEqual(written, [str(resources)])
        self.assertEqual(
            json.loads(resources.read_text())["servers"][0]["admin_timeout"], 300.0
        )
        log.network.warning.assert_not_called()

    def test_comm_config_check_skips_a_path_the_worker_cannot_enter(self):
        with _cannot_enter(HOST_PREFIX):
            changed = network_tasks._ensure_client_comm_config_portable(
                HOST_PREFIX + "SwarmMed/prod_00/node-B"
            )

        self.assertFalse(changed)

    def test_server_starts_when_the_host_path_is_under_a_folder_the_worker_cannot_enter(
        self,
    ):
        resources = self.prod_dir / "server" / "local" / "resources.json"
        at_launch = []

        with (
            patch.dict(os.environ, {FLARE_ADMIN_TIMEOUT_ENV: "300"}),
            _cannot_enter(HOST_PREFIX),
        ):
            runs = self._start(
                on_docker_run=lambda _command: at_launch.append(
                    json.loads(resources.read_text())["servers"][0]["admin_timeout"]
                ),
                bind_source=_under_host_prefix,
                role="server",
            )

        self.assertEqual(self.network.status, "RUNNING")
        self.assertEqual(len(runs), 1)
        # The host path is not visible here, so the server mounts the Hub's
        # volumes, where the timeout was written.
        self.assertIn("--volumes-from", runs[0])
        self.assertEqual(at_launch, [300.0])

    def test_client_starts_when_the_host_path_is_under_a_folder_the_worker_cannot_enter(
        self,
    ):
        _add_flare_files(self.prod_dir)

        with _cannot_enter(HOST_PREFIX):
            runs = self._start(
                bind_source=_under_host_prefix, role="client", participants=["node-B"]
            )

        self.assertEqual(self.network.status, "RUNNING")
        self.assertEqual(len(runs), 1)
        self.assertIn("--volumes-from", runs[0])
        self.assertEqual(_internal_host(self.prod_dir / "node-B"), "localhost")

    def test_standby_starts_when_the_host_path_is_under_a_folder_the_worker_cannot_enter(
        self,
    ):
        with (
            patch.dict(os.environ, {FLARE_ADMIN_TIMEOUT_ENV: "300"}),
            _cannot_enter(HOST_PREFIX),
        ):
            runs = self._failover(
                start_server=True, clients=("node-B",), bind_source=_under_host_prefix
            )

        self.assertEqual(self.network.status, "RUNNING")
        record = json.loads((self.prod_dir / ACTIVE_SERVER_FILE).read_text())
        self.assertEqual(record["server_host"], "192.0.2.10")
        self.assertTrue(record["server_started_here"])
        self.assertEqual(len(runs), 2)
        self.assertIn("swarmmedhub.role=server", runs[0])
        self.assertIn("swarmmedhub.role=client", runs[1])
        self.assertTrue(runs[1][runs[1].index("--name") + 1].endswith("node-B"))


class ImportHandOverTests(RuntimeFixture, TestCase):
    """What the CLI writes as root, the worker (appuser) must be able to use and delete."""

    def setUp(self):
        super().setUp()
        _add_flare_files(self.prod_dir)
        # FLARE provisioning writes the start scripts executable.
        for script in self.prod_dir.glob("*/startup/*.sh"):
            script.chmod(0o755)
        self.network_dir = network_prod_dir(self.network).parents[2]

    def _package(self, site=None):
        """The network's startup package, or the kit of one site in it."""
        package = create_startup_kits_zip(self.network)
        if not site:
            return package
        with zipfile.ZipFile(package) as bundle:
            return io.BytesIO(bundle.read(f"{site}.zip"))

    def _import(self, package):
        """Import a package into a Hub that has none of the network's files."""
        SwarmNetwork.objects.filter(pk=self.network.pk).delete()
        shutil.rmtree(self.network_dir)
        package.seek(0)
        self.network = network_services.import_network(
            project=self.project,
            actor=self.user,
            name="Imported",
            package_source=package,
        )
        self.assertEqual(network_prod_dir(self.network).parents[2], self.network_dir)

    def test_import_as_root_hands_every_file_to_the_worker(self):
        with _as_root() as chown:
            self._import(self._package())

        self.assertEqual(_not_handed_over(chown, self.network_dir), [])

    def test_import_as_root_hands_the_projects_folder_to_the_worker(self):
        project_dir = self.network_dir.parent
        other_network = project_dir / "other-network"
        (other_network / "startup").mkdir(parents=True)
        (other_network / "startup" / "fed_client.json").write_text("{}")

        with _as_root() as chown:
            self._import(self._package())

        handed = _handed_over(chown)
        self.assertEqual(handed.count(project_dir), 1)
        # Not the project's other networks, and not workspaces/ itself.
        self.assertEqual(_tree(other_network) & set(handed), set())
        self.assertNotIn(project_dir.parent, handed)
        self.assertEqual(project_dir.parent, Path(settings.BASE_DIR) / "workspaces")

    def test_create_as_root_hands_the_projects_folder_to_the_worker(self):
        def provision(*, network_id, **_kwargs):
            kit = (
                Path(settings.BASE_DIR)
                / "workspaces"
                / str(self.project.identifier)
                / str(network_id)
                / "workspace"
                / "demo_study"
                / "prod_00"
                / "server"
                / "startup"
            )
            kit.mkdir(parents=True)
            (kit / "fed_server.json").write_text("{}")

        with (
            _as_root() as chown,
            patch.object(
                network_services, "generate_flare_startup_kit", side_effect=provision
            ),
        ):
            network = network_services.create_network(
                project=self.project,
                actor=self.user,
                name="Created",
                participants=[{"name": "node-C", "ip": "100.64.0.3"}],
                server_ip="100.64.0.1",
                include_local_client=False,
            )

        network_dir = network_prod_dir(network).parents[2]
        handed = _handed_over(chown)
        self.assertEqual(_not_handed_over(chown, network_dir), [])
        self.assertIn(network_dir.parent, handed)
        self.assertNotIn(network_dir.parent.parent, handed)

    def test_a_failed_import_still_hands_over_what_it_wrote(self):
        with (
            _as_root() as chown,
            patch.object(
                network_services,
                "move_kit_folder",
                side_effect=OSError("No space left on device"),
            ),
            self.assertRaisesRegex(OSError, "No space left"),
        ):
            self._import(self._package())

        written = _tree(self.network_dir)
        # The package's files and at least one site's kit were written.
        self.assertIn(self.network_dir / "node-A.zip", written)
        self.assertTrue(any(path.name == "fed_client.json" for path in written))
        self.assertEqual(_not_handed_over(chown, self.network_dir), [])

    def test_import_keeps_the_start_scripts_executable(self):
        self._import(self._package())

        scripts = sorted(self.prod_dir.glob("*/startup/*.sh"))
        self.assertEqual(
            {script.parent.parent.name for script in scripts},
            {"server", "node-A", "node-B", "admin-node-A@nvidia.com"},
        )
        for script in scripts:
            with self.subTest(script=str(script.relative_to(self.prod_dir))):
                self.assertTrue(os.access(script, os.X_OK))
        for name in ("client.key", "fed_client.json", "rootCA.pem"):
            with self.subTest(name=name):
                self.assertFalse(
                    os.access(self.prod_dir / "node-A" / "startup" / name, os.X_OK)
                )

    def test_a_single_sites_kit_keeps_its_scripts_executable(self):
        self._import(self._package(site="node-A"))

        # prod_00 is the site's workspace; the server's kit stays in the
        # network's folder.
        for script in (
            self.prod_dir / "startup" / "sub_start.sh",
            self.prod_dir / "startup" / "start.sh",
            self.network_dir / "server_startup" / "sub_start.sh",
        ):
            with self.subTest(script=str(script.relative_to(self.network_dir))):
                self.assertTrue(os.access(script, os.X_OK))
        self.assertFalse(os.access(self.prod_dir / "startup" / "client.key", os.X_OK))

    def test_cli_import_then_start_failover_and_standby_with_the_appuser_worker(self):
        # The documented CLI imports the package as root.
        with _as_root() as chown:
            self._import(self._package())
        self.assertEqual(_not_handed_over(chown, self.network_dir), [])

        # The worker, which now owns the files, as appuser does after the
        # hand-over, starts the site's client, hosts the standby server and
        # switches again: with the checkout under a folder it cannot enter on
        # the Docker host, and with the default runtime image, whose build
        # folder it writes into prod_00.
        build_dir = self.prod_dir / ".swarmmedhub_runtime_build"
        with (
            _cannot_enter(HOST_PREFIX),
            patch.object(
                network_tasks,
                "collect_project_runtime_requirements",
                return_value=["nvflare==2.7.1"],
            ),
            patch.object(network_tasks, "_build_image_with_compat") as build,
        ):
            client_runs = self._start(
                bind_source=_under_host_prefix,
                image=None,
                role="client",
                participants=["node-B"],
            )
            self.assertEqual(self.network.status, "RUNNING")
            self.assertEqual(len(client_runs), 1)
            self.assertTrue((build_dir / "Dockerfile").is_file())
            self.assertTrue((build_dir / "requirements.txt").is_file())
            build.assert_called()

            standby_runs = self._failover(
                start_server=True, bind_source=_under_host_prefix, image=None
            )
            self.assertEqual(self.network.status, "RUNNING")
            record = json.loads((self.prod_dir / ACTIVE_SERVER_FILE).read_text())
            self.assertEqual(record["server_host"], "192.0.2.10")
            self.assertTrue(record["server_started_here"])
            self.assertTrue((self.prod_dir / "server" / "transfer").is_dir())
            self.assertEqual(len(standby_runs), 2)
            self.assertIn("swarmmedhub.role=server", standby_runs[0])

            switch_runs = self._failover(
                start_server=False,
                server_host="198.51.100.7",
                servers=("server",),
                bind_source=_under_host_prefix,
                image=None,
            )
            self.assertEqual(self.network.status, "RUNNING")
            record = json.loads((self.prod_dir / ACTIVE_SERVER_FILE).read_text())
            self.assertEqual(record["server_host"], "198.51.100.7")
            self.assertEqual(len(switch_runs), 1)
            self.assertIn("server:198.51.100.7", switch_runs[0])


class ScriptTests(SimpleTestCase):
    def test_a_script_the_worker_cannot_make_executable_names_the_remedy(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "sub_start.sh"
            script.write_text("#!/bin/bash\n")
            script.chmod(0o644)
            real_chmod = os.chmod

            def chmod(path, *args, **kwargs):
                # As for appuser and a script that the CLI wrote as root.
                if os.fspath(path) == str(script):
                    raise PermissionError(errno.EPERM, "Operation not permitted", path)
                return real_chmod(path, *args, **kwargs)

            with (
                patch.object(os, "chmod", side_effect=chmod),
                self.assertRaises(PermissionError) as raised,
            ):
                network_tasks._ensure_executable(str(script))

        message = str(raised.exception)
        self.assertIn(str(script), message)
        self.assertIn(f"uid {os.getuid()}", message)
        self.assertIn("docker compose restart celery_worker", message)
