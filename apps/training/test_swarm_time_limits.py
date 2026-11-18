"""Tests for the environment overrides of the swarm workflow's time limits."""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from .services import (
    RESUME_START_TASK_TIMEOUT,
    resume_timeout_hint,
    swarm_time_limits_for_job,
    swarm_time_limits_from_env,
)

START_TIMEOUT_ENV = "SWARMMEDHUB_SWARM_START_TASK_TIMEOUT"


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

    def test_start_timeout_is_a_whole_number_of_seconds(self):
        for raw in ("120", "120.0", " 120 ", "1.2e2"):
            with (
                self.subTest(raw=raw),
                patch.dict("os.environ", {START_TIMEOUT_ENV: raw}, clear=True),
            ):
                limits = swarm_time_limits_from_env()
                self.assertEqual(limits, {"start_task_timeout": 120})
                # NVFlare rejects a float as a task timeout.
                self.assertIs(type(limits["start_task_timeout"]), int)

    def test_invalid_start_timeouts_are_ignored_with_a_warning(self):
        for raw in ("120.5", "0", "-10", "two minutes", "nan", "inf"):
            log = MagicMock()
            with (
                self.subTest(raw=raw),
                patch.dict("os.environ", {START_TIMEOUT_ENV: raw}, clear=True),
            ):
                self.assertEqual(swarm_time_limits_from_env(log), {})
                log.training.warning.assert_called_once()
                self.assertIn(
                    "positive whole number of seconds",
                    log.training.warning.call_args.args[0],
                )

    def test_the_other_limits_keep_fractional_seconds(self):
        env = {
            "SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL": "90.5",
            START_TIMEOUT_ENV: "30",
        }
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(
                swarm_time_limits_from_env(),
                {"max_status_report_interval": 90.5, "start_task_timeout": 30},
            )


class JobTimeLimitTests(SimpleTestCase):
    def test_new_job_keeps_the_nvflare_start_timeout(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(swarm_time_limits_for_job(resumed=False), ({}, ""))

    def test_resumed_job_waits_longer_for_the_workflow_to_start(self):
        with patch.dict("os.environ", {}, clear=True):
            limits, note = swarm_time_limits_for_job(resumed=True)

        self.assertEqual(RESUME_START_TASK_TIMEOUT, 600)
        self.assertEqual(limits, {"start_task_timeout": 600})
        self.assertIs(type(limits["start_task_timeout"]), int)
        self.assertIn("default for a resumed job", note)

    def test_the_variable_wins_over_the_resume_default(self):
        with patch.dict("os.environ", {START_TIMEOUT_ENV: "120"}, clear=True):
            self.assertEqual(
                swarm_time_limits_for_job(resumed=True),
                ({"start_task_timeout": 120}, ""),
            )

    def test_the_variable_also_sets_a_new_job(self):
        with patch.dict("os.environ", {START_TIMEOUT_ENV: "30"}, clear=True):
            self.assertEqual(
                swarm_time_limits_for_job(resumed=False),
                ({"start_task_timeout": 30}, ""),
            )

    def test_an_invalid_variable_leaves_the_resume_default(self):
        log = MagicMock()
        with patch.dict("os.environ", {START_TIMEOUT_ENV: "1.5"}, clear=True):
            limits, note = swarm_time_limits_for_job(resumed=True, log=log)

        self.assertEqual(limits, {"start_task_timeout": RESUME_START_TASK_TIMEOUT})
        self.assertTrue(note)
        log.training.warning.assert_called_once()

    def test_the_other_limits_apply_to_a_resumed_job_as_well(self):
        env = {"SWARMMEDHUB_SWARM_PROGRESS_TIMEOUT": "7200"}
        with patch.dict("os.environ", env, clear=True):
            limits, _note = swarm_time_limits_for_job(resumed=True)

        self.assertEqual(
            limits,
            {
                "progress_timeout": 7200.0,
                "start_task_timeout": RESUME_START_TASK_TIMEOUT,
            },
        )


class ResumeTimeoutHintTests(SimpleTestCase):
    def test_names_both_timeouts_while_the_admin_timeout_is_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            hint = resume_timeout_hint()

        self.assertIn("ships its checkpoint", hint)
        self.assertIn("SWARMMEDHUB_FLARE_ADMIN_TIMEOUT", hint)
        self.assertIn("restart the network", hint)
        self.assertIn(f"{START_TIMEOUT_ENV} (now 600 s)", hint)

    def test_asks_for_a_restart_only_once_the_job_has_failed(self):
        # The note follows the submission, and restarting the network at the Hub
        # that runs the FL server would end the job just submitted.
        with patch.dict("os.environ", {}, clear=True):
            hint = resume_timeout_hint()

        self.assertIn("If this job fails", hint)
        self.assertIn("which ends running jobs", hint)
        # A resumed job that failed before its first round has no checkpoint.
        self.assertIn("resume the interrupted job again", hint)

    def test_shows_the_start_timeout_a_resumed_job_gets(self):
        with patch.dict("os.environ", {START_TIMEOUT_ENV: "900"}, clear=True):
            self.assertIn(f"{START_TIMEOUT_ENV} (now 900 s)", resume_timeout_hint())

    def test_left_out_once_the_admin_timeout_is_set(self):
        env = {"SWARMMEDHUB_FLARE_ADMIN_TIMEOUT": "300"}
        with patch.dict("os.environ", env, clear=True):
            self.assertEqual(resume_timeout_hint(), "")

    def test_kept_while_the_admin_timeout_is_invalid(self):
        # The FL server ignores such a value, so the default still applies.
        env = {"SWARMMEDHUB_FLARE_ADMIN_TIMEOUT": "five minutes"}
        with patch.dict("os.environ", env, clear=True):
            self.assertIn("SWARMMEDHUB_FLARE_ADMIN_TIMEOUT", resume_timeout_hint())
