"""Tests for the environment overrides of the swarm workflow's time limits."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from .services import (
    RESUME_JOB_STATUS_CHECK_INTERVAL,
    RESUME_START_TASK_TIMEOUT,
    resume_timeout_hint,
    swarm_time_limits_for_job,
    swarm_time_limits_from_env,
    write_swarm_time_limits,
)

START_TIMEOUT_ENV = "SWARMMEDHUB_SWARM_START_TASK_TIMEOUT"
SWARM_CONTROLLER = "nvflare.app_common.ccwf.swarm_server_ctl.SwarmServerController"


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
        self.assertEqual(
            limits, {"start_task_timeout": 600, "job_status_check_interval": 10.0}
        )
        self.assertIs(type(limits["start_task_timeout"]), int)
        self.assertIn("default for a resumed job", note)

    def test_resumed_job_checks_the_status_reports_after_every_site_can_report(self):
        # FLARE checks every site's last status report 2 s after the workflow has
        # started; the starting site cannot report while it starts the workflow.
        with patch.dict("os.environ", {}, clear=True):
            limits, note = swarm_time_limits_for_job(resumed=True)

        self.assertEqual(RESUME_JOB_STATUS_CHECK_INTERVAL, 10.0)
        self.assertEqual(limits["job_status_check_interval"], 10.0)
        self.assertIn("job_status_check_interval=10 s", note)

    def test_the_variable_wins_over_the_resume_default(self):
        with patch.dict("os.environ", {START_TIMEOUT_ENV: "120"}, clear=True):
            limits, note = swarm_time_limits_for_job(resumed=True)

        self.assertEqual(
            limits, {"start_task_timeout": 120, "job_status_check_interval": 10.0}
        )
        self.assertNotIn("start_task_timeout", note)
        self.assertIn("job_status_check_interval", note)

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

        self.assertEqual(
            limits,
            {
                "start_task_timeout": RESUME_START_TASK_TIMEOUT,
                "job_status_check_interval": RESUME_JOB_STATUS_CHECK_INTERVAL,
            },
        )
        self.assertIn("default for a resumed job", note)
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
                "job_status_check_interval": RESUME_JOB_STATUS_CHECK_INTERVAL,
            },
        )


class ExportedJobTimeLimitTests(SimpleTestCase):
    """write_swarm_time_limits, which edits the server app of an exported job."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.job = Path(temp_dir.name)
        self.config_path = self.job / "app_server/config/config_fed_server.json"
        self.config_path.parent.mkdir(parents=True)
        self.config = {
            "format_version": 2,
            "workflows": [
                {"id": "swarm", "path": SWARM_CONTROLLER, "args": {"num_rounds": 5}},
                {"id": "other", "path": "custom.OtherController", "args": {}},
            ],
        }
        self.config_path.write_text(json.dumps(self.config, indent=4))

    def test_every_limit_is_written_to_the_swarm_controller(self):
        # Values equal to NVIDIA FLARE 2.7.1's defaults, which its export leaves out.
        limits = {
            "start_task_timeout": 10,
            "max_status_report_interval": 90.0,
            "progress_timeout": 3600.0,
        }

        self.assertEqual(write_swarm_time_limits(self.job, limits), ["app_server"])

        workflows = json.loads(self.config_path.read_text())["workflows"]
        self.assertEqual(workflows[0]["args"], {"num_rounds": 5, **limits})
        self.assertIs(type(workflows[0]["args"]["start_task_timeout"]), int)
        self.assertEqual(workflows[1], self.config["workflows"][1])

    def test_nothing_chosen_leaves_the_job_as_exported(self):
        before = self.config_path.read_text()

        self.assertEqual(write_swarm_time_limits(self.job, {}), [])
        self.assertEqual(self.config_path.read_text(), before)


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
