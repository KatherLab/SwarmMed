"""A default installation: the Celery worker runs as appuser, the CLI as root.

The worker cannot enter every folder on the Docker host's path to the checkout,
such as /root, and cannot change what the CLI wrote as root unless the CLI
handed it over.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess  # nosec B404
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from django.test import TestCase

from . import tasks as network_tasks
from .failover import (
    ACTIVE_SERVER_FILE,
    FLARE_ADMIN_TIMEOUT_ENV,
    apply_flare_admin_timeout,
)
from .test_failover import NetworkFixture, _add_flare_files, _internal_host

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
