"""Tests for the network app."""

from django.test import SimpleTestCase

from .tasks import (
    _client_runtime_env_pairs,
    _get_runtime_manifest_base_url,
    _split_runtime_entrypoint,
)


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

        self.assertEqual(manifest_url, "http://swarmmedhub:8000")

    def test_runtime_manifest_base_url_uses_public_proxy_on_host_network(self):
        manifest_url = _get_runtime_manifest_base_url(
            use_host_network=True, remote_host="100.100.101.102"
        )

        self.assertEqual(manifest_url, "https://100.100.101.102:5085")

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
        self.assertIn(("SCRATCH_DIR", "/mnt/dlhd0/deploy_test_duke_iid/swarmed_cli_mst"), pairs)
        self.assertIn(("SCRATCHDIR", "/mnt/dlhd0/deploy_test_duke_iid/swarmed_cli_mst"), pairs)
        self.assertIn(("MODEL_NAME", "MST"), pairs)
        self.assertIn(("CONFIG", "unilateral"), pairs)
        self.assertIn(("TRAINING_MODE", "swarm"), pairs)
