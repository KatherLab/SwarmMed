"""Stopping a network also stops its unfinished training jobs."""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from project.models import Project
from training.models import TrainingJob

from .models import SwarmNetwork
from .tasks import stop_swarm_network_task


class StopNetworkJobsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="stopper", password="test-password"
        )  # nosec B106
        project = Project.objects.create(title="Stop Project", author=self.user)
        self.network = SwarmNetwork.objects.create(
            name="Stop Network", project=project, author=self.user, status="RUNNING"
        )
        self.running = TrainingJob.objects.create(
            project=project, network=self.network, status="RUNNING"
        )
        self.finished = TrainingJob.objects.create(
            project=project, network=self.network, status="COMPLETED"
        )

    def test_unfinished_jobs_are_stopped_and_finished_ones_kept(self):
        with patch("network.tasks._stop_labeled_runtime"), patch(
            "network.tasks.get_logger", return_value=MagicMock()
        ):
            stop_swarm_network_task(str(self.network.identifier), self.user.id)

        self.network.refresh_from_db()
        self.running.refresh_from_db()
        self.finished.refresh_from_db()
        self.assertEqual(self.network.status, "STOPPED")
        self.assertEqual(self.running.status, "STOPPED")
        self.assertIsNotNone(self.running.completed_at)
        self.assertEqual(self.finished.status, "COMPLETED")
