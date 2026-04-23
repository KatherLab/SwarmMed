"""Tests for the training app."""

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from django.test import SimpleTestCase

from .flare_adapter import (
    _build_manifest_request_url,
    _get_manifest_discovery_targets,
    _get_manifest_verify_value,
    receive_model,
)
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
