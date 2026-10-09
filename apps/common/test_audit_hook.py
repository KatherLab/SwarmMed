"""Regression tests for the audit entry written when a platform model is saved.

The hook called RequestContextMiddleware.get_context(), which does not exist, so
every save logged 'Auto audit log failed' and wrote no audit entry.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from logs.context import set_context
from network.models import SwarmNetwork
from project.models import Project
from training.models import TrainingJob


class AuditHookTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username="author", password="test-password"
        )  # nosec B106
        self.logger = MagicMock()
        for target, value in (
            ("logs.logger.get_logger", MagicMock(return_value=self.logger)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(set_context)

    def _messages(self):
        return [call.kwargs["message"] for call in self.logger.log.call_args_list]

    def test_save_writes_an_audit_entry_attributed_to_the_author(self):
        with patch("common.signals._logger.warning") as failure:
            project = Project.objects.create(
                title="Audited Project", author=self.author
            )

        failure.assert_not_called()
        self.assertIn("Project CREATED: Audited Project", " | ".join(self._messages()))
        entry = next(
            call
            for call in self.logger.log.call_args_list
            if call.kwargs["message"].startswith("Project CREATED")
        )
        self.assertEqual(entry.kwargs["user"], self.author)
        self.assertEqual(entry.kwargs["object_id"], str(project.identifier))

    def test_user_from_the_task_context_takes_precedence(self):
        operator = User.objects.create_user(
            username="operator", password="test-password"
        )  # nosec B106
        set_context(user=operator)
        Project.objects.create(title="Context Project", author=self.author)
        entry = next(
            call
            for call in self.logger.log.call_args_list
            if call.kwargs["message"].startswith("Project CREATED")
        )
        self.assertEqual(entry.kwargs["user"], operator)

    def test_progress_only_saves_are_not_audited(self):
        project = Project.objects.create(title="Progress Project", author=self.author)
        network = SwarmNetwork.objects.create(
            name="Progress Network",
            project=project,
            author=self.author,
            status="RUNNING",
        )
        job = TrainingJob.objects.create(
            project=project, network=network, status="RUNNING"
        )
        self.logger.log.reset_mock()

        job.progress_percent = 40
        job.rounds_finished = 8
        job.save(
            update_fields=["progress_percent", "rounds_finished", "progress_updated_at"]
        )
        self.assertEqual(self._messages(), [])

        job.status = "COMPLETED"
        job.save(update_fields=["status", "progress_percent"])
        self.assertEqual(len(self._messages()), 1)
        self.assertTrue(self._messages()[0].startswith("TrainingJob UPDATED"))
