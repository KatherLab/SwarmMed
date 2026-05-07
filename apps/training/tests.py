"""Tests for the training app."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings

from network.models import SwarmNetwork, SwarmParticipant
from project.models import Project

from . import services as training_services
from .flare_adapter import (
    _build_manifest_request_url,
    _get_manifest_discovery_targets,
    _get_manifest_verify_value,
    receive_model,
)
from .models import TrainingJob
from .utils import (
    extract_total_rounds_from_flare_config,
    infer_training_terminal_status,
    scrape_docker_progress,
    summarize_training_log,
)


class FlareAdapterManifestTests(SimpleTestCase):
    """Regression coverage for runtime manifest discovery."""

    def test_manifest_request_url_appends_project_id(self):
        url = _build_manifest_request_url(
            "http://swarmmedhub:8000", "project-123"
        )

        self.assertEqual(
            url,
            "http://swarmmedhub:8000/data/manifest/?project_id=project-123",
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
                "SWARMMEDHUB_MANIFEST_URL": "http://swarmmedhub:8000",
                "SWARMMEDHUB_SERVER_HOST": "100.100.101.102",
                "DOCKER_HOST_IP": "172.17.0.1",
            },
            clear=False,
        ):
            targets = _get_manifest_discovery_targets()

        self.assertEqual(targets[0], "http://swarmmedhub:8000")
        self.assertIn("https://100.100.101.102:5085", targets)
        self.assertIn("http://app:8000", targets)

    def test_manifest_verify_value_skips_tls_checks_for_http(self):
        self.assertFalse(
            _get_manifest_verify_value("http://swarmmedhub:8000/data/manifest/")
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

    def test_summarize_training_log_extracts_server_finished_round_status(self):
        summary = summarize_training_log(
            "updated status of client node-A on round 1: "
            "timestamp=2026-05-04 15:48:49, action=finished_learn_task, "
            "all_done=False"
        )

        self.assertEqual(summary["rounds_finished"], 1)
        self.assertIsNone(summary["terminal_status"])

    def test_per_round_training_log_is_not_treated_as_terminal(self):
        summary = summarize_training_log(
            "Training finished for round. Sending updates to server...\n"
        )

        self.assertIsNone(summary["terminal_status"])

    def test_nvflare_server_completion_markers_are_terminal(self):
        log_text = """
        Workflow controller finished on all clients
        Workflow controller done
        Server runner finished
        """

        self.assertEqual(infer_training_terminal_status(log_text), "COMPLETED")

    def test_extract_total_rounds_accepts_generated_controller_id(self):
        config = {
            "workflows": [
                {
                    "id": "controller",
                    "path": "nvflare.app_common.ccwf.swarm_server_ctl.SwarmServerController",
                    "args": {"num_rounds": 2},
                }
            ]
        }

        self.assertEqual(extract_total_rounds_from_flare_config(config), 2)

    @patch("training.utils.shutil.which", return_value="docker")
    @patch("training.utils.subprocess.run")
    def test_docker_scrape_scopes_terminal_status_to_latest_job(
        self, mock_run, _mock_which
    ):
        old_job = "11111111-1111-1111-1111-111111111111"
        new_job = "22222222-2222-2222-2222-222222222222"

        def fake_run(args, **_kwargs):
            if args[1] == "ps":
                return SimpleNamespace(
                    returncode=0,
                    stdout="swarm-test-server\n",
                    stderr="",
                )
            if args[1] == "logs":
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        f"Job: {old_job} started to run\n"
                        "Aborting current RUN due to FATAL_SYSTEM_ERROR\n"
                        f"Job: {new_job} started to run\n"
                        f"[identity=server, run={new_job}] Waiting for clients\n"
                    ),
                    stderr="",
                )
            raise AssertionError(args)

        mock_run.side_effect = fake_run

        results = scrape_docker_progress(participant_ids=["server"])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["job_id"], new_job)
        self.assertIsNone(results[0]["terminal_status"])
        self.assertFalse(results[0]["ended"])


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

    def test_log_completion_sets_finished_eta_and_persists_completed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            job = TrainingJob.objects.create(
                project=self.project,
                network=self.network,
                status="RUNNING",
                flare_job_id="66666666-6666-6666-6666-666666666666",
                flare_job_uuid="66666666-6666-6666-6666-666666666666",
                total_rounds=20,
            )
            log_dir = (
                Path(tmpdir)
                / "workspaces"
                / str(self.project.identifier)
                / str(self.network.identifier)
                / "workspace"
                / "prod_00"
                / job.flare_job_uuid
                / "app_server"
            )
            log_dir.mkdir(parents=True)
            (log_dir / "log.txt").write_text(
                "Workflow controller finished on all clients\n"
                "Workflow controller done\n"
                "Server runner finished\n",
                encoding="utf-8",
            )

            with override_settings(BASE_DIR=tmpdir):
                payload = training_services.get_training_status_payload(
                    network=self.network, job=job
                )

            job.refresh_from_db()

        self.assertEqual(payload["status"], "Completed")
        self.assertEqual(payload["progress"], 100)
        self.assertEqual(payload["eta"], "Finished")
        self.assertEqual(job.status, "COMPLETED")
        self.assertEqual(job.progress_percent, 100)

    def test_completed_job_does_not_revert_to_running(self):
        job = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="COMPLETED",
            progress_percent=100,
            flare_job_id="77777777-7777-7777-7777-777777777777",
            flare_job_uuid="77777777-7777-7777-7777-777777777777",
        )

        training_services.persist_training_job_state(
            job,
            status="RUNNING",
            progress_percent=45,
        )
        job.refresh_from_db()

        self.assertEqual(job.status, "COMPLETED")
        self.assertEqual(job.progress_percent, 100)


class TrainingViewServiceParityTests(TestCase):
    """The training_api_state and training_api_results views must delegate
    to ``serialize_training_job_state`` and ``find_local_global_model_file``
    so any future CLI command that needs the same payload gets identical
    results.
    """

    @staticmethod
    def _fake_log():
        def no_op(*_args, **_kwargs):
            return None

        return SimpleNamespace(log=no_op)

    def setUp(self):
        self.user = User.objects.create_user(
            username="train-parity", password="test-password"
        )  # nosec B106
        self.flare_uuid = "abcd1234-abcd-1234-abcd-1234abcd1234"
        with patch("logs.signals.get_logger", return_value=self._fake_log()):
            self.project = Project.objects.create(
                title="Parity Project", author=self.user
            )
            self.network = SwarmNetwork.objects.create(
                name="Parity Network",
                project=self.project,
                author=self.user,
                status="RUNNING",
            )
            self.job = TrainingJob.objects.create(
                project=self.project,
                network=self.network,
                status="RUNNING",
                flare_job_id=f"Submitted job: {self.flare_uuid}",
                flare_job_uuid=self.flare_uuid,
                total_rounds=5,
                rounds_finished=2,
                progress_percent=40,
            )
        self.participant = SwarmParticipant.objects.create(
            network=self.network,
            user=self.user,
            role="CLIENT",
            participant_id="site-a",
            ip="127.0.0.1",
        )

    def test_serialize_training_job_state_returns_compact_payload(self):
        payload = training_services.serialize_training_job_state(self.job)
        self.assertEqual(
            set(payload.keys()),
            {
                "flare_job_id",
                "flare_job_uuid",
                "status",
                "total_rounds",
                "rounds_finished",
                "progress_percent",
                "created_at",
            },
        )
        self.assertEqual(payload["flare_job_uuid"], self.flare_uuid)
        self.assertEqual(payload["progress_percent"], 40)

    def test_find_local_global_model_prefers_canonical_filename(self):
        with tempfile.TemporaryDirectory() as base_dir:
            workspace = Path(base_dir) / "workspaces" / str(
                self.project.identifier
            ) / str(self.network.identifier) / "workspace" / f"job-{self.flare_uuid}"
            workspace.mkdir(parents=True)
            # Drop both a legacy and the canonical file in the same dir.
            (workspace / "best_FL_model.pt").write_bytes(b"legacy")
            (workspace / "FL_global_model.pt").write_bytes(b"canonical")

            with override_settings(BASE_DIR=base_dir):
                resolved = training_services.find_local_global_model_file(self.job)

            self.assertIsNotNone(resolved)
            self.assertTrue(resolved.endswith("FL_global_model.pt"))

    def test_find_local_global_model_falls_back_to_legacy_filename(self):
        with tempfile.TemporaryDirectory() as base_dir:
            workspace = Path(base_dir) / "workspaces" / str(
                self.project.identifier
            ) / str(self.network.identifier) / "workspace" / f"job-{self.flare_uuid}"
            workspace.mkdir(parents=True)
            (workspace / "global_model.pt").write_bytes(b"legacy")

            with override_settings(BASE_DIR=base_dir):
                resolved = training_services.find_local_global_model_file(self.job)

            self.assertIsNotNone(resolved)
            self.assertTrue(resolved.endswith("global_model.pt"))

    def test_find_local_global_model_returns_none_when_workspace_empty(self):
        with tempfile.TemporaryDirectory() as base_dir:
            with override_settings(BASE_DIR=base_dir):
                resolved = training_services.find_local_global_model_file(self.job)
        self.assertIsNone(resolved)

    def test_sync_completed_job_local_results_uses_canonical_key_layout(self):
        class FakeStorage:
            def __init__(self):
                self.saved = {}

            def exists(self, name):
                return name in self.saved

            def save(self, name, content):
                self.saved[name] = content.read()
                return name

        self.job.status = "COMPLETED"
        self.job.save(update_fields=["status"])
        storage = FakeStorage()

        with tempfile.TemporaryDirectory() as base_dir:
            workspace = (
                Path(base_dir)
                / "workspaces"
                / str(self.project.identifier)
                / str(self.network.identifier)
                / "workspace"
                / "site-a"
                / self.flare_uuid
                / "models"
            )
            workspace.mkdir(parents=True)
            artifact = workspace / "model_weights.npz"
            artifact.write_bytes(b"weights")

            with override_settings(BASE_DIR=base_dir), patch(
                "training.services.default_storage", storage
            ):
                synced_keys = training_services.sync_completed_job_local_results(
                    self.job, network=self.network
                )

        expected_key = (
            f"{self.project.identifier}/results/"
            f"{self.flare_uuid}/site-a/models/model_weights.npz"
        )
        self.assertEqual(synced_keys, [expected_key])
        self.assertEqual(storage.saved[expected_key], b"weights")
