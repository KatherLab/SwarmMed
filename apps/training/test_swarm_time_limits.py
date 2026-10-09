"""Tests for the environment overrides of the swarm workflow's time limits."""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from .services import swarm_time_limits_from_env


class SwarmTimeLimitTests(SimpleTestCase):
    def test_nothing_set_keeps_nvflare_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(swarm_time_limits_from_env(), {})

    def test_set_values_become_controller_arguments(self):
        env = {
            "SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL": "600",
            "SWARMMEDHUB_SWARM_PROGRESS_TIMEOUT": "14400.5",
        }
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(
                swarm_time_limits_from_env(),
                {"max_status_report_interval": 600.0, "progress_timeout": 14400.5},
            )

    def test_invalid_values_are_ignored_with_a_warning(self):
        log = MagicMock()
        env = {
            "SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL": "ten minutes",
            "SWARMMEDHUB_SWARM_PROGRESS_TIMEOUT": "-5",
        }
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(swarm_time_limits_from_env(log), {})
        self.assertEqual(log.training.warning.call_count, 2)
