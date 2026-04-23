"""Tests for the network app."""

from django.test import SimpleTestCase

from .tasks import (
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

        self.assertEqual(manifest_url, "http://medswarmhub:8000")

    def test_runtime_manifest_base_url_uses_public_proxy_on_host_network(self):
        manifest_url = _get_runtime_manifest_base_url(
            use_host_network=True, remote_host="100.100.101.102"
        )

        self.assertEqual(manifest_url, "https://100.100.101.102:5085")
