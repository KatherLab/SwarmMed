"""Tests for the training app."""

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from network.models import SwarmNetwork
from project.models import Project

from . import services as training_services
from .flare_adapter import (
    _build_manifest_request_url,
    _get_manifest_discovery_targets,
    _get_manifest_verify_value,
    receive_model,
)
from .models import TrainingJob
from .utils import infer_training_terminal_status, summarize_training_log


class FlareAdapterManifestTests(SimpleTestCase):
    """Regression coverage for runtime manifest discovery."""

    def test_manifest_request_url_appends_project_id(self):
        url = _build_manifest_request_url(
            "http://medswarmhub:8000", "project-123"
        )

        self.assertEqual(
            url,
            "http://medswarmhub:8000/data/manifest/?project_id=project-123",
        )

    def test_manifest_request_url_preserves_existing_manifest_path(self):
        url = _build_manifest_request_url(
            "https://100.100.101.102:5085/data/manifest", "project-123"
        )

        self.assertEqual(
            url,
            "https://100.100.101.102:5085/data/manifest?project_id=project-123",
        )

    def test_manifest_discovery_targets_prioritize_explicit_url(self):
        with patch.dict(
            "os.environ",
            {
                "MEDSWARMHUB_MANIFEST_URL": "http://medswarmhub:8000",
                "MEDSWARMHUB_SERVER_HOST": "100.100.101.102",
                "DOCKER_HOST_IP": "172.17.0.1",
            },
            clear=False,
        ):
            targets = _get_manifest_discovery_targets()

        self.assertEqual(targets[0], "http://medswarmhub:8000")
        self.assertIn("https://100.100.101.102:5085", targets)
        self.assertIn("http://app:8000", targets)

    def test_manifest_verify_value_skips_tls_checks_for_http(self):
        self.assertFalse(
            _get_manifest_verify_value("http://medswarmhub:8000/data/manifest/")
        )


class TrainingLogSummaryTests(SimpleTestCase):
    """Regression coverage for training terminal state inference."""

    def test_infer_failed_status_overrides_completion_markers(self):
        log_text = """
        2026-04-23 10:00:00,000 - server - INFO - ending workflow
        2026-04-23 10:00:01,000 - server - ERROR - EXECUTION_EXCEPTION
        """

        self.assertEqual(infer_training_terminal_status(log_text), "FAILED")

    def test_summarize_training_log_extracts_round_and_completion(self):
        summary = summarize_training_log(
            "Round 3 | Log Loss: 0.25\nchild worker process finished\n"
        )

        self.assertEqual(summary["rounds_finished"], 3)
        self.assertEqual(summary["terminal_status"], "COMPLETED")

    def test_per_round_training_log_is_not_treated_as_terminal(self):
        summary = summarize_training_log(
            "Training finished for round. Sending updates to server...\n"
        )

        self.assertIsNone(summary["terminal_status"])


class FlareAdapterReceiveModelTests(SimpleTestCase):
    """Regression coverage for numpy-based model transport."""

    @patch("apps.training.flare_adapter.flare.receive")
    def test_receive_model_preserves_numpy_key_payload(self, mock_receive):
        payload = SimpleNamespace(
            params={"numpy_key": np.array([1.0, 2.0], dtype=np.float32)}
        )
        mock_receive.return_value = payload

        model = receive_model()

        self.assertIn("numpy_key", model.params)


class TrainingIdentityServiceTests(TestCase):
    """Regression coverage for canonical FLARE job identity handling."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="trainer", password="test-password"
        )  # nosec B106
        self.project = Project.objects.create(title="Train Project", author=self.user)
        self.network = SwarmNetwork.objects.create(
            name="Train Network",
            project=self.project,
            author=self.user,
            status="RUNNING",
        )

    def test_get_training_job_resolves_by_canonical_flare_uuid(self):
        job = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id="Submitted job: 11111111-1111-1111-1111-111111111111",
            flare_job_uuid="11111111-1111-1111-1111-111111111111",
        )

        resolved = training_services.get_training_job(
            network=self.network,
            identifier="11111111-1111-1111-1111-111111111111",
        )

        self.assertEqual(resolved.id, job.id)

    def test_ensure_training_job_reuses_existing_canonical_row(self):
        existing = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id="Submitted job: 22222222-2222-2222-2222-222222222222",
            flare_job_uuid="22222222-2222-2222-2222-222222222222",
        )

        job, created = training_services.ensure_training_job(
            network=self.network,
            flare_job_id="22222222-2222-2222-2222-222222222222",
            status="RUNNING",
        )

        self.assertFalse(created)
        self.assertEqual(job.id, existing.id)
        self.assertEqual(
            TrainingJob.objects.filter(network=self.network).count(),
            1,
        )

    def test_persist_training_job_state_preserves_stopped_progress(self):
        job = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id="33333333-3333-3333-3333-333333333333",
            flare_job_uuid="33333333-3333-3333-3333-333333333333",
            progress_percent=45,
        )

        training_services.persist_training_job_state(
            job,
            status="STOPPED",
            progress_percent=job.progress_percent,
        )
        job.refresh_from_db()

        self.assertEqual(job.status, "STOPPED")
        self.assertEqual(job.progress_percent, 45)

    def test_sync_job_from_remote_payload_updates_existing_job(self):
        job = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id="Submitted job: 44444444-4444-4444-4444-444444444444",
            flare_job_uuid="44444444-4444-4444-4444-444444444444",
            rounds_finished=1,
            progress_percent=20,
        )

        synced = training_services.sync_job_from_remote_payload(
            network=self.network,
            remote_job={
                "flare_job_id": "44444444-4444-4444-4444-444444444444",
                "status": "COMPLETED",
                "total_rounds": 5,
                "rounds_finished": 4,
                "progress_percent": 100,
            },
        )
        job.refresh_from_db()

        self.assertEqual(synced.id, job.id)
        self.assertEqual(job.status, "COMPLETED")
        self.assertEqual(job.progress_percent, 100)
        self.assertEqual(job.rounds_finished, 4)

    @patch("training.services.build_training_status_payload")
    def test_get_training_status_payload_persists_terminal_status(self, mock_build):
        job = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id="55555555-5555-5555-5555-555555555555",
            flare_job_uuid="55555555-5555-5555-5555-555555555555",
        )
        mock_build.return_value = {
            "status": "Completed",
            "progress": 100,
            "duration": "21s",
            "eta": "0s",
            "job_id": job.flare_job_uuid,
            "created_at": job.created_at.isoformat(),
        }

        payload = training_services.get_training_status_payload(
            network=self.network, job=job
        )
        job.refresh_from_db()

        self.assertEqual(payload["status"], "Completed")
        self.assertEqual(job.status, "COMPLETED")
        self.assertEqual(job.progress_percent, 100)
        self.assertIsNotNone(job.completed_at)
