"""Regression tests: the periodic monitor must leave finished training jobs alone.

It used to open an admin session for every COMPLETED job every minute (on a shared
host the retries reached other FL servers), re-upload each finished job's results
every minute, and overwrite the job's completion time each time.
"""

import os
import tempfile
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from network.models import SwarmNetwork
from project.models import Project

from . import services as training_services
from . import tasks
from .models import TrainingJob

JOB_UUID = "22222222-2222-2222-2222-222222222222"


class MonitorFinishedJobsTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.user = User.objects.create_user(
            username="monitor", password="test-password"
        )  # nosec B106
        self.project = Project.objects.create(title="Monitor Project", author=self.user)
        self.network = SwarmNetwork.objects.create(
            name="Monitor Network",
            project=self.project,
            author=self.user,
            status="STOPPED",
        )
        # The layout the monitor walks: workspaces/<project>/<network>/.../prod_00
        prod = os.path.join(
            self.tmp.name,
            "workspaces",
            str(self.project.identifier),
            str(self.network.identifier),
            "workspace",
            "study",
            "prod_00",
        )
        app_dir = os.path.join(prod, JOB_UUID, "app_site-1")
        os.makedirs(app_dir)
        with open(os.path.join(app_dir, "FL_global_model.pt"), "wb") as handle:
            handle.write(b"weights")
        with open(os.path.join(prod, JOB_UUID, "log.txt"), "w") as handle:
            handle.write("round 1 done\n")

        for target, value in (
            ("training.tasks.logger", MagicMock()),
            ("network.utils.get_tailscale_ip", MagicMock(return_value="127.0.0.1")),
            ("training.utils.scrape_docker_progress", MagicMock(return_value=[])),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _job(self, status, completed_at=None):
        return TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status=status,
            flare_job_id=JOB_UUID,
            flare_job_uuid=JOB_UUID,
            total_rounds=2,
            completed_at=completed_at,
        )

    def _run_monitor(self, *, uploaded, log_status=None):
        session = MagicMock()
        session.api.do_command.return_value = {}
        with (
            override_settings(BASE_DIR=self.tmp.name),
            patch(
                "training.tasks.resolve_admin_session_target",
                return_value=("admin@site-1", "/tmp/admin", "127.0.0.1"),
            ) as resolve,
            patch(
                "training.tasks.new_secure_session_with_host", return_value=session
            ) as new_session,
            patch("training.tasks.parse_nvflare_jobs", return_value=[]),
            patch(
                "training.tasks.summarize_training_log",
                return_value={"rounds_finished": 2, "terminal_status": log_status},
            ),
            patch("training.tasks._results_already_uploaded", return_value=uploaded),
            patch("training.tasks.upload_folder_to_s3") as upload,
        ):
            tasks.monitor_training_jobs()
        return resolve, new_session, upload

    def test_completed_job_with_stored_results_is_not_polled_or_reuploaded(self):
        finished = timezone.now() - timedelta(days=3)
        job = self._job("COMPLETED", completed_at=finished)

        resolve, new_session, upload = self._run_monitor(uploaded=True)

        new_session.assert_not_called()
        resolve.assert_not_called()
        upload.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "COMPLETED")
        self.assertEqual(job.completed_at, finished)

    def test_completed_job_without_stored_results_is_synced_once_keeping_its_time(self):
        finished = timezone.now() - timedelta(days=3)
        job = self._job("COMPLETED", completed_at=finished)

        _resolve, new_session, upload = self._run_monitor(uploaded=False)

        new_session.assert_not_called()
        upload.assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.completed_at, finished)

    def test_running_job_that_finished_is_synced_and_gets_a_completion_time(self):
        job = self._job("RUNNING")
        before = timezone.now()

        _resolve, new_session, upload = self._run_monitor(
            uploaded=False, log_status="COMPLETED"
        )

        new_session.assert_called_once()
        upload.assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.status, "COMPLETED")
        self.assertIsNotNone(job.completed_at)
        self.assertGreaterEqual(job.completed_at, before)


class CompletionTimeTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="stamp", password="test-password")  # nosec B106
        project = Project.objects.create(title="Stamp Project", author=user)
        network = SwarmNetwork.objects.create(
            name="Stamp Network", project=project, author=user, status="RUNNING"
        )
        self.job = TrainingJob.objects.create(
            project=project, network=network, status="PENDING"
        )

    def test_starting_a_job_does_not_stamp_a_completion_time(self):
        training_services.persist_training_job_state(self.job, status="RUNNING")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "RUNNING")
        self.assertIsNone(self.job.completed_at)

    def test_finishing_a_job_stamps_a_completion_time(self):
        training_services.persist_training_job_state(self.job, status="RUNNING")
        training_services.persist_training_job_state(self.job, status="COMPLETED")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "COMPLETED")
        self.assertIsNotNone(self.job.completed_at)
