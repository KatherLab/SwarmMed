"""Every FLARE admin session that the Hub opens is closed, however its use ends.

An admin session that is left open keeps FLARE's connection threads running. A
CLI process then never exits: `swarmed training stop` printed the stopped job and
hung, and so did a `training start` or `training resume` whose submission failed.
The worker and the web process kept the threads of every session that failed.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from network.models import SwarmNetwork
from project.models import Project

from . import runtime as training_runtime
from . import services as training_services
from . import tasks
from .models import TrainingJob
from .test_resume import SubmitHarness

JOB_UUID = "33333333-3333-3333-3333-333333333333"
ADMIN_TARGET = ("admin@nvidia.com", "/nonexistent/admin", "192.0.2.10")


class RunningJobTestCase(TestCase):
    """A running network with a running job, and a fake admin session."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username="operator", password="test-password"
        )  # nosec B106
        self.project = Project.objects.create(title="Session Project", author=self.user)
        self.network = SwarmNetwork.objects.create(
            name="Session Network",
            project=self.project,
            author=self.user,
            status="RUNNING",
        )
        self.job = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id=JOB_UUID,
            flare_job_uuid=JOB_UUID,
            total_rounds=4,
        )
        self.session = MagicMock()


class StopSessionTests(RunningJobTestCase):
    def _stop(self):
        with (
            patch.object(
                training_services,
                "resolve_admin_session_target",
                return_value=ADMIN_TARGET,
            ),
            patch.object(
                training_services,
                "new_secure_session_with_host",
                return_value=self.session,
            ),
        ):
            return training_services.stop_training_job(
                actor=self.user, network=self.network, job=self.job
            )

    def test_stop_closes_the_admin_session(self):
        job = self._stop()

        self.session.api.do_command.assert_called_once_with(f"abort_job {JOB_UUID}")
        self.session.close.assert_called_once_with()
        self.assertEqual(job.status, "STOPPED")

    def test_stop_closes_the_session_when_the_abort_fails(self):
        self.session.api.do_command.side_effect = RuntimeError("boom")

        with self.assertRaisesMessage(RuntimeError, "boom"):
            self._stop()

        self.session.close.assert_called_once_with()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "RUNNING")

    def test_a_logout_that_fails_does_not_fail_the_stop(self):
        self.session.close.side_effect = RuntimeError("Server did not respond")

        job = self._stop()

        self.assertEqual(job.status, "STOPPED")


class SubmitSessionTests(SubmitHarness, TestCase):
    def test_failed_submit_closes_the_session(self):
        self.session.submit_job.side_effect = RuntimeError("submission refused")

        with self.assertRaisesMessage(RuntimeError, "submission refused"):
            training_services.submit_training_job(actor=self.user, network=self.network)

        self.session.close.assert_called_once_with()

    def test_a_job_that_cannot_be_exported_closes_the_session(self):
        with (
            patch.object(
                training_services,
                "write_swarm_time_limits",
                side_effect=OSError("No space left on device"),
            ),
            self.assertRaisesMessage(OSError, "No space left on device"),
        ):
            training_services.submit_training_job(actor=self.user, network=self.network)

        self.session.submit_job.assert_not_called()
        self.session.close.assert_called_once_with()

    def test_a_submission_closes_its_session_once(self):
        job = training_services.submit_training_job(
            actor=self.user, network=self.network
        )

        self.assertEqual(job.status, "RUNNING")
        self.session.close.assert_called_once_with()


class MonitorSessionTests(RunningJobTestCase):
    def setUp(self):
        super().setUp()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.base_dir = temp_dir.name
        # The monitor polls a running job of a network whose folder holds prod_00.
        os.makedirs(
            os.path.join(
                self.base_dir,
                "workspaces",
                str(self.project.identifier),
                str(self.network.identifier),
                "workspace",
                "study",
                "prod_00",
                JOB_UUID,
            )
        )
        for target, value in (
            ("training.tasks.logger", MagicMock()),
            (
                "network.overlay.get_local_addresses",
                MagicMock(return_value=frozenset({"127.0.0.1"})),
            ),
            ("training.utils.scrape_docker_progress", MagicMock(return_value=[])),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_monitor_closes_the_session_when_list_jobs_raises(self):
        discovery, poll = MagicMock(), MagicMock()
        for session in (discovery, poll):
            session.api.do_command.side_effect = RuntimeError("Server did not respond")

        with (
            override_settings(BASE_DIR=self.base_dir),
            patch.object(
                tasks, "resolve_admin_session_target", return_value=ADMIN_TARGET
            ),
            patch.object(
                tasks, "new_secure_session_with_host", side_effect=[discovery, poll]
            ) as new_session,
        ):
            tasks.monitor_training_jobs()

        # One session to discover the network's jobs, one to poll the running job.
        self.assertEqual(new_session.call_count, 2)
        discovery.close.assert_called_once_with()
        poll.close.assert_called_once_with()


class StatusSessionTests(RunningJobTestCase):
    def test_status_check_closes_the_session_when_list_jobs_raises(self):
        self.session.api.do_command.side_effect = RuntimeError("Server did not respond")

        with (
            patch.object(
                training_runtime,
                "resolve_admin_session_target",
                return_value=ADMIN_TARGET,
            ),
            patch.object(
                training_runtime,
                "new_secure_session_with_host",
                return_value=self.session,
            ),
        ):
            self.assertIsNone(training_runtime.nvflare_status_payload(self.network))

        self.session.close.assert_called_once_with()
