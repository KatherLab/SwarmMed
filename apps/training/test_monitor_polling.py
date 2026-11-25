"""The job monitor runs one pass at a time.

Celery beat starts a pass every minute, and a pass can take longer, for example
while an FL server does not answer. Passes that overlapped, in one worker process
or in several, raced on the same job records and could upload a finished job's
results twice.
"""

from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings

from network.models import SwarmNetwork
from project.models import Project

from . import tasks


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "monitor-lock",
        }
    }
)
class MonitorLockTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        user = User.objects.create_user(
            username="monitor-lock", password="test-password"
        )  # nosec B106
        project = Project.objects.create(title="Lock Project", author=user)
        # A running network, whose discovery asks for the admin session's target
        # once per pass; no job, so nothing else does.
        SwarmNetwork.objects.create(
            name="Lock Network", project=project, author=user, status="RUNNING"
        )
        self.log = MagicMock()
        self.resolve = MagicMock(return_value=None)
        for target, value in (
            (
                "training.tasks.logger",
                MagicMock(get_logger=MagicMock(return_value=self.log)),
            ),
            ("training.tasks.resolve_admin_session_target", self.resolve),
            ("training.utils.scrape_docker_progress", MagicMock(return_value=[])),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_pass_that_is_due_while_another_runs_does_nothing(self):
        def start_the_next_pass_once(_network):
            self.resolve.side_effect = None
            tasks.monitor_training_jobs()

        self.resolve.side_effect = start_the_next_pass_once

        tasks.monitor_training_jobs()

        self.assertEqual(self.resolve.call_count, 1)
        self.assertIn(
            "Monitor: the previous pass is still running; skipping this one.",
            [call.args[0] for call in self.log.training.debug.call_args_list],
        )

    def test_a_pass_that_fails_releases_the_lock(self):
        with (
            patch(
                "training.tasks.stall_warning_minutes",
                side_effect=RuntimeError("the pass failed"),
            ),
            self.assertRaisesMessage(RuntimeError, "the pass failed"),
        ):
            tasks.monitor_training_jobs()

        tasks.monitor_training_jobs()

        self.resolve.assert_called_once()

    def test_a_pass_runs_without_the_lock_when_the_cache_fails(self):
        with patch.object(cache, "add", side_effect=ConnectionError("no cache")):
            tasks.monitor_training_jobs()

        self.resolve.assert_called_once()

    def test_the_lock_is_scoped_to_the_database(self):
        # A test run beside a live Hub can share the Hub's cache, never its
        # database: neither holds up the other's passes.
        cache.add("training:monitor_training_jobs:the_live_hubs_database", "hub", 600)

        tasks.monitor_training_jobs()

        self.resolve.assert_called_once()
        self.assertIn(settings.DATABASES["default"]["NAME"], tasks._monitor_lock_key())
