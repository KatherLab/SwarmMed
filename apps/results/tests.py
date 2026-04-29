"""Tests for canonical results ownership and visualization lookups."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from network.models import SwarmNetwork
from project.models import Project
from training.models import TrainingJob

from .models import ResultsVisualizationRun, TrainingResult
from .services import (
    download_results_to_directory,
    get_results_visualization_run,
    list_results,
    start_results_visualization,
)
from .tasks import sync_project_results


class _FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **_kwargs):
        return self.pages


class _FakeS3Client:
    def __init__(self, pages):
        self.pages = pages
        self.downloaded = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _FakePaginator(self.pages)

    def download_file(self, _bucket, key, target):
        self.downloaded.append((key, target))
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text("weights", encoding="utf-8")

    def list_objects_v2(self, **_kwargs):
        contents = []
        for page in self.pages:
            contents.extend(page.get("Contents", []))
        return {"Contents": contents}


@override_settings(AWS_STORAGE_BUCKET_NAME="test-bucket")
class ResultsOwnershipTests(TestCase):
    """Regression coverage for canonical FLARE job result ownership."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="results-user", password="test-password"
        )  # nosec B106
        self.project = Project.objects.create(
            title="Results Project", author=self.user
        )
        self.network = SwarmNetwork.objects.create(
            name="Results Network",
            project=self.project,
            author=self.user,
            status="RUNNING",
        )
        self.flare_uuid = "77777777-7777-7777-7777-777777777777"
        self.job = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="COMPLETED",
            flare_job_id=f"Submitted job: {self.flare_uuid}",
            flare_job_uuid=self.flare_uuid,
        )
        self.result_key = (
            f"{self.project.identifier}/results/{self.flare_uuid}/site-a/FL_global_model.pt"
        )
        self.script_key = (
            f"{self.project.identifier}/code/results_visualization/summary.py"
        )

    def _build_s3(self, *, include_script=False):
        contents = [
            {
                "Key": self.result_key,
                "Size": 123,
                "ETag": '"0123456789abcdef0123456789abcdef"',
            }
        ]
        if include_script:
            contents.append({"Key": self.script_key, "Size": 21})
        return _FakeS3Client([{"Contents": contents}])

    def test_sync_project_results_is_idempotent_and_uses_canonical_job(self):
        s3 = self._build_s3()

        with patch("results.tasks.get_s3_client", return_value=s3), patch(
            "results.tasks._sync_local_workspace_results"
        ):
            sync_project_results(str(self.project.identifier))
            sync_project_results(str(self.project.identifier))

        results = list(TrainingResult.objects.filter(job=self.job))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].file_path, self.result_key)
        self.assertEqual(results[0].file_size, 123)

    def test_list_and_download_results_accept_canonical_flare_uuid(self):
        TrainingResult.objects.create(
            job=self.job,
            file_path=self.result_key,
            file_size=123,
        )
        s3 = self._build_s3()

        with patch("results.services.get_s3_client", return_value=s3):
            rows = list_results(self.project, job_identifier=self.flare_uuid)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_identifier"], str(self.job.identifier))
        self.assertEqual(rows[0]["flare_job_id"], self.flare_uuid)
        self.assertEqual(rows[0]["md5"], "0123456789abcdef0123456789abcdef")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("results.services.get_s3_client", return_value=s3):
                payload = download_results_to_directory(
                    self.project,
                    output_dir=tmpdir,
                    job_identifier=self.flare_uuid,
                )

            downloaded = Path(payload["downloaded_files"][0])
            self.assertTrue(downloaded.exists())
            self.assertEqual(downloaded.read_text(encoding="utf-8"), "weights")
            self.assertEqual(
                payload["checksums"][str(downloaded)],
                hashlib.md5(b"weights").hexdigest(),  # nosec B324
            )
            self.assertEqual(
                payload["global_model_md5s"],
                [hashlib.md5(b"weights").hexdigest()],  # nosec B324
            )

    def test_result_services_ignore_non_global_checkpoints(self):
        s3 = _FakeS3Client(
            [
                {
                    "Contents": [
                        {"Key": self.result_key, "Size": 123},
                        {
                            "Key": (
                                f"{self.project.identifier}/results/"
                                f"{self.flare_uuid}/site-a/last_global_model.ckpt"
                            ),
                            "Size": 456,
                        },
                    ]
                }
            ]
        )

        with patch("results.services.get_s3_client", return_value=s3):
            rows = list_results(self.project, job_identifier=self.flare_uuid)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_path"], self.result_key)

    def test_results_visualization_uses_canonical_job_scope(self):
        prior_run = ResultsVisualizationRun.objects.create(
            project=self.project,
            job=None,
            flare_job_id=self.flare_uuid,
            user=self.user,
            status="pending",
            celery_task_id="old-task",
        )
        s3 = self._build_s3(include_script=True)

        with patch("results.services.get_s3_client", return_value=s3), patch(
            "results.services.run_results_visualization_task.delay",
            return_value=SimpleNamespace(id="new-task"),
        ), patch("results.services.current_app.control.revoke") as revoke:
            run = start_results_visualization(
                project=self.project,
                user=self.user,
                job_identifier=self.flare_uuid,
            )

        prior_run.refresh_from_db()
        self.assertEqual(prior_run.status, "cancelled")
        revoke.assert_called_once_with("old-task", terminate=True)
        self.assertEqual(run.job_id, self.job.id)
        self.assertEqual(run.flare_job_id, self.flare_uuid)

        resolved = get_results_visualization_run(
            self.project, job_identifier=self.flare_uuid
        )
        self.assertEqual(resolved.id, run.id)
