"""Regression tests: the periodic monitor must leave finished training jobs alone.

It used to open an admin session for every COMPLETED job every minute (on a shared
host the retries reached other FL servers), re-upload each finished job's results
every minute, and overwrite the job's completion time each time.
"""

import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from network.models import SwarmNetwork
from project.models import Project

from . import resume as training_resume
from . import services as training_services
from . import tasks
from .models import TrainingJob

JOB_UUID = "22222222-2222-2222-2222-222222222222"
OTHER_JOB_UUID = "99999999-9999-9999-9999-999999999999"


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
            (
                "network.overlay.get_local_addresses",
                MagicMock(return_value=frozenset({"127.0.0.1"})),
            ),
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


class MonitorTotalRoundsTests(TestCase):
    """A site's monitor records only the job's own total of rounds, never a guess."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base_dir = tmp.name
        user = User.objects.create_user(
            username="totals", password="test-password"
        )  # nosec B106
        self.project = Project.objects.create(title="Totals Project", author=user)
        self.network = SwarmNetwork.objects.create(
            name="Totals Network", project=self.project, author=user, status="RUNNING"
        )
        self.prod = (
            Path(tmp.name)
            / "workspaces"
            / str(self.project.identifier)
            / str(self.network.identifier)
            / "workspace"
            / "study"
            / "prod_00"
        )
        # A site whose FL client runs the job; its log shows two rounds.
        self.job_dir = self.prod / "node-B" / JOB_UUID
        (self.job_dir / "app_node-B" / "config").mkdir(parents=True)
        (self.job_dir / "log.txt").write_text("Finished round 2\n")
        self.job = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id=JOB_UUID,
            flare_job_uuid=JOB_UUID,
        )
        for target, value in (
            ("training.tasks.logger", MagicMock()),
            (
                "network.overlay.get_local_addresses",
                MagicMock(return_value=frozenset()),
            ),
            ("training.utils.scrape_docker_progress", MagicMock(return_value=[])),
            (
                "training.tasks.resolve_admin_session_target",
                MagicMock(return_value=None),
            ),
            (
                "training.tasks.summarize_training_log",
                MagicMock(
                    return_value={"rounds_finished": 2, "terminal_status": None}
                ),
            ),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _monitored_job(self):
        with override_settings(BASE_DIR=self.base_dir):
            tasks.monitor_training_jobs()
        self.job.refresh_from_db()
        return self.job

    def test_a_client_only_site_stores_no_guessed_total(self):
        job = self._monitored_job()

        self.assertEqual(job.rounds_finished, 2)
        self.assertIsNone(job.total_rounds)
        self.assertIsNone(job.progress_percent)

    def test_another_jobs_server_config_is_not_used(self):
        config = self.prod / "server" / OTHER_JOB_UUID / "app_server" / "config"
        config.mkdir(parents=True)
        (config / "config_fed_server.json").write_text(
            json.dumps(
                {
                    "workflows": [
                        {
                            "id": "swarm_controller",
                            "path": "nvflare.app_common.ccwf.swarm_server_ctl."
                            "SwarmServerController",
                            "args": {"num_rounds": 30},
                        }
                    ]
                }
            )
        )

        job = self._monitored_job()

        self.assertIsNone(job.total_rounds)
        self.assertIsNone(job.progress_percent)

    def test_the_total_is_read_from_the_jobs_run_file(self):
        training_resume.write_run_file(
            self.job_dir,
            network_id=self.network.identifier,
            total_rounds=8,
            start_round=0,
        )

        job = self._monitored_job()

        self.assertEqual(job.total_rounds, 8)
        self.assertEqual(job.progress_percent, 37)  # rounds 0-2 of 8


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
