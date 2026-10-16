"""Tests for the network app."""

import json
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase, override_settings

from project.models import Project

from swarmed_cli.bootstrap import _configure_host_cli_environment

from .models import SwarmNetwork, SwarmParticipant
from .provision import safe_participant_name
from .tasks import (
    _client_runtime_env_pairs,
    _detect_docker_gpu_support,
    _ensure_client_comm_config_portable,
    _get_runtime_manifest_base_url,
    _read_local_hostname_candidates,
    _read_project_client_ip_map,
    _resolve_bind_source_path,
    _runtime_ulimit_args,
    _should_use_client_host_network,
    _split_runtime_entrypoint,
)
from .utils import ensure_worker_writable


class RuntimeLaunchTests(SimpleTestCase):
    """Regression coverage for FLARE runtime container launch args."""

    def test_split_runtime_entrypoint_promotes_shell_command(self):
        entrypoint, args = _split_runtime_entrypoint(
            ["/bin/bash", "-lc", "cd /workspace/startup && ./sub_start.sh"]
        )

        self.assertEqual(entrypoint, "/bin/bash")
        self.assertEqual(args, ["-lc", "cd /workspace/startup && ./sub_start.sh"])

    def test_split_runtime_entrypoint_keeps_plain_command_untouched(self):
        command = ["python", "-m", "example"]

        entrypoint, args = _split_runtime_entrypoint(command)

        self.assertIsNone(entrypoint)
        self.assertEqual(args, command)

    def test_runtime_manifest_base_url_uses_internal_app_on_bridge_network(self):
        manifest_url = _get_runtime_manifest_base_url(use_host_network=False)

        self.assertEqual(manifest_url, "https://swarmmedhub:5085")

    def test_runtime_manifest_base_url_uses_public_proxy_on_host_network(self):
        manifest_url = _get_runtime_manifest_base_url(
            use_host_network=True, remote_host="100.100.101.102"
        )

        self.assertEqual(manifest_url, "https://100.100.101.102:5085")

    def test_runtime_ulimit_args_defaults_to_higher_nofile_limit(self):
        self.assertEqual(
            _runtime_ulimit_args({}),
            ["--ulimit", "nofile=65536:65536"],
        )

    def test_runtime_ulimit_args_can_be_disabled(self):
        self.assertEqual(
            _runtime_ulimit_args({"SWARMMEDHUB_RUNTIME_NOFILE_LIMIT": "0"}),
            [],
        )

    def test_resolve_bind_source_path_keeps_existing_local_path_when_remap_missing(self):
        existing_path = Path(settings.BASE_DIR) / "apps" / "network" / "tasks.py"

        with patch.dict("os.environ", {"HOST_PROJECT_PATH": "/definitely/missing"}):
            resolved = _resolve_bind_source_path(str(existing_path))

        self.assertEqual(resolved, str(existing_path.resolve()))

    @override_settings(BASE_DIR="/app")
    def test_resolve_bind_source_path_remaps_container_app_path(self):
        with patch.dict("os.environ", {"HOST_PROJECT_PATH": "/host/repo"}):
            resolved = _resolve_bind_source_path("/app/workspaces/demo")

        self.assertEqual(resolved, "/host/repo/workspaces/demo")

    def test_client_runtime_env_pairs_pass_site_local_training_config(self):
        pairs = _client_runtime_env_pairs(
            {
                "SITE_NAME": "node_A",
                "INSTITUTION": "node_A",
                "DATA_DIR": "/mnt/dlhd0/DUKE_iid",
                "SCRATCH_DIR": "/mnt/dlhd0/deploy_test_duke_iid/swarmed_cli_mst",
                "MODEL_NAME": "MST",
                "EPOCHS_PER_ROUND": "5",
            }
        )

        self.assertIn(("SITE_NAME", "node_A"), pairs)
        self.assertIn(("INSTITUTION", "node_A"), pairs)
        self.assertIn(("DATA_DIR", "/mnt/dlhd0/DUKE_iid"), pairs)
        self.assertIn(("DATADIR", "/mnt/dlhd0/DUKE_iid"), pairs)
        self.assertIn(
            ("SCRATCH_DIR", "/mnt/dlhd0/deploy_test_duke_iid/swarmed_cli_mst"),
            pairs,
        )
        self.assertIn(
            ("SCRATCHDIR", "/mnt/dlhd0/deploy_test_duke_iid/swarmed_cli_mst"),
            pairs,
        )
        self.assertIn(("MODEL_NAME", "MST"), pairs)
        self.assertIn(("CONFIG", "unilateral"), pairs)
        self.assertIn(("TRAINING_MODE", "swarm"), pairs)

    def test_safe_participant_name_converts_underscore_for_nvflare(self):
        self.assertEqual(safe_participant_name("node_A"), "node-A")

    def test_local_participant_candidate_matches_hyphenated_flare_name(self):
        with patch.dict(
            "os.environ",
            {
                "SWARMMEDHUB_LOCAL_PARTICIPANT": "node_A",
                "SWARMMEDHUB_HOSTNAME": "",
            },
            clear=False,
        ):
            candidates = _read_local_hostname_candidates()

        self.assertIn("node_A", candidates)
        self.assertIn("node-A", candidates)

    def test_ensure_worker_writable_adds_owner_write_permission(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir) / "workspace"
            workspace.mkdir()
            runtime_file = workspace / "runtime_requirements.txt"
            runtime_file.write_text("nvflare==2.7.1\n")
            workspace.chmod(0o500)
            runtime_file.chmod(0o400)

            ensure_worker_writable(workspace)

            self.assertTrue(workspace.stat().st_mode & stat.S_IWUSR)
            self.assertTrue(runtime_file.stat().st_mode & stat.S_IWUSR)

    def test_read_project_client_ip_map_supports_client_ip_and_legacy_listening_host(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_yml = Path(tmp_dir) / "project.yml"
            project_yml.write_text(
                "participants:\n"
                "  - name: node-A\n"
                "    type: client\n"
                "    client_ip: 100.64.0.1\n"
                "  - name: node-B\n"
                "    type: client\n"
                "    listening_host: 100.64.0.2\n"
            )
            ip_map = _read_project_client_ip_map(str(project_yml))
            self.assertEqual(
                ip_map,
                {"node-A": "100.64.0.1", "node-B": "100.64.0.2"},
            )

    def test_ensure_client_comm_config_portable_normalizes_external_ip_to_localhost(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            local_dir = Path(tmp_dir) / "local"
            local_dir.mkdir()
            comm_cfg = local_dir / "comm_config.json"
            comm_cfg.write_text(
                json.dumps(
                    {
                        "internal": {
                            "scheme": "tcp",
                            "resources": {
                                "host": "100.100.101.102",
                                "connection_security": "clear",
                            },
                        }
                    }
                )
            )
            changed = _ensure_client_comm_config_portable(tmp_dir)
            self.assertTrue(changed)
            updated = json.loads(comm_cfg.read_text())
            self.assertEqual(
                updated["internal"]["resources"]["host"], "localhost"
            )
            self.assertFalse(_ensure_client_comm_config_portable(tmp_dir))

    def test_should_use_client_host_network_auto_disables_on_docker_desktop(self):
        self.assertFalse(
            _should_use_client_host_network(
                {},
                docker_info={"OperatingSystem": "Docker Desktop"},
            )
        )
        self.assertFalse(
            _should_use_client_host_network(
                {},
                docker_info={"KernelVersion": "6.6.12-linuxkit"},
            )
        )
        self.assertTrue(
            _should_use_client_host_network(
                {},
                docker_info={"OperatingSystem": "Ubuntu 24.04 LTS"},
            )
        )
        self.assertTrue(
            _should_use_client_host_network(
                {"SWARMMEDHUB_CLIENT_HOST_NETWORK": "true"},
                docker_info={"OperatingSystem": "Docker Desktop"},
            )
        )

    def test_detect_docker_gpu_support_uses_probe_when_nvidia_runtime_unregistered(self):
        with patch("network.tasks.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            self.assertTrue(
                _detect_docker_gpu_support(
                    docker_path="/usr/bin/docker",
                    image_name="demo:latest",
                    env={},
                    docker_info={"Runtimes": {"runc": {}}},
                )
            )
            mock_run.assert_called_once()

    def test_configure_host_cli_environment_falls_back_to_localhost_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir)
            ca_file = repo / ".secrets" / "certs" / "internal" / "ca.crt"
            ca_file.parent.mkdir(parents=True)
            ca_file.write_text("dummy-ca")
            env = {
                "DB_HOST": "postgres",
                "POSTGRES_HOST_PORT": "55432",
                "REDIS_HOST": "redis",
                "AWS_S3_ENDPOINT_URL": "https://minio:9000",
                "DOCKER_HOST_IP": "127.0.0.1",
            }
            with patch("swarmed_cli.bootstrap._can_resolve_host", return_value=False):
                _configure_host_cli_environment(repo, env, in_container=False)

            self.assertEqual(env["DB_HOST"], "127.0.0.1")
            self.assertEqual(env["DB_PORT"], "55432")
            self.assertEqual(env["REDIS_HOST"], "127.0.0.1")
            self.assertEqual(env["AWS_S3_ENDPOINT_URL"], "https://127.0.0.1:9000")
            self.assertEqual(env["CA_CERT_PATH"], str(ca_file))


class GossipParticipantNormalizationTests(TestCase):
    """The gossip endpoint receives the original site name (e.g. ``node_A``)
    in the X-Gossip-Participant header and request body, but the DB stores
    the FLARE-safe form (``node-A``). The endpoint must normalise both forms
    so peers using either spelling are accepted as the same identity.
    """

    @staticmethod
    def _fake_logger():
        def no_op(*_args, **_kwargs):
            return None

        return SimpleNamespace(
            network=SimpleNamespace(
                debug=no_op,
                error=no_op,
                info=no_op,
                warning=no_op,
            ),
            log=no_op,
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username="gossip-user", password="test-password"
        )  # nosec B106
        with patch("logs.signals.get_logger", return_value=self._fake_logger()):
            self.project = Project.objects.create(
                title="Gossip Project", author=self.user
            )
            self.network = SwarmNetwork.objects.create(
                name="Gossip Network",
                project=self.project,
                author=self.user,
                status="RUNNING",
                gossip_token="t0ken123456",
            )
        self.participant = SwarmParticipant.objects.create(
            network=self.network,
            user=self.user,
            participant_id="node-A",  # FLARE-safe form, the canonical DB value
            role="CLIENT",
            ip="127.0.0.1",
        )

    def _post(self, *, header_value: str, body_value: str, status: str = "RUNNING"):
        client = Client()
        with patch("network.views.logger", self._fake_logger()):
            return client.post(
                f"/network/api/gossip/{self.network.identifier}/",
                data=json.dumps(
                    {
                        "participant_id": body_value,
                        "source_participant_id": body_value,
                        "status": status,
                    }
                ),
                content_type="application/json",
                HTTP_X_GOSSIP_PARTICIPANT=header_value,
                HTTP_X_GOSSIP_TOKEN="t0ken123456",
            )

    def test_safe_form_round_trip(self):
        response = self._post(header_value="node-A", body_value="node-A")
        self.assertEqual(response.status_code, 200)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.status, "RUNNING")

    def test_underscore_form_is_normalized_and_accepted(self):
        # Peer config still uses node_A; normalisation must let it through.
        response = self._post(header_value="node_A", body_value="node_A")
        self.assertEqual(response.status_code, 200, response.content)
        self.participant.refresh_from_db()
        self.assertEqual(self.participant.status, "RUNNING")

    def test_mixed_forms_still_authenticate(self):
        # Some peers write the safe name in the header but the original site
        # name in the body (or vice-versa). Both must reach the same row.
        response = self._post(header_value="node_A", body_value="node-A")
        self.assertEqual(response.status_code, 200, response.content)
