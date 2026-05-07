"""Tests for canonical results ownership and visualization lookups."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import reverse

from network.models import SwarmNetwork
from project.models import Project, UserCurrentProject
from training.models import TrainingJob

from .models import ResultsVisualizationPlot, ResultsVisualizationRun, TrainingResult
from .services import (
    build_results_dashboard_context,
    download_results_to_directory,
    get_results_visualization_run,
    list_results,
    resolve_results_visualization_plot,
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


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    AWS_STORAGE_BUCKET_NAME="test-bucket",
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
)
class ResultsOwnershipTests(TestCase):
    """Regression coverage for canonical FLARE job result ownership."""

    @staticmethod
    def _fake_log():
        def no_op(*_args, **_kwargs):
            return None

        return SimpleNamespace(
            auth=SimpleNamespace(info=no_op),
            results=SimpleNamespace(
                debug=no_op,
                error=no_op,
                info=no_op,
                warning=no_op,
            )
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username="results-user", password="test-password"
        )  # nosec B106
        self.user.profile.accepted_terms = True
        self.user.profile.accepted_policy = True
        self.user.profile.save()
        self.project = Project.objects.create(
            title="Results Project", author=self.user
        )
        UserCurrentProject.objects.create(
            user=self.user, project=self.project
        )
        with patch("users.signals.get_logger", return_value=self._fake_log()):
            self.client.force_login(self.user)
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
        checkpoint_key = (
            f"{self.project.identifier}/results/"
            f"{self.flare_uuid}/site-a/model_weights.npz"
        )
        s3 = _FakeS3Client(
            [
                {
                    "Contents": [
                        {"Key": self.result_key, "Size": 123},
                        {"Key": checkpoint_key, "Size": 456},
                    ]
                }
            ]
        )

        with patch("results.tasks.get_s3_client", return_value=s3), patch(
            "results.tasks._sync_local_workspace_results"
        ), patch(
            "results.tasks.logger.get_logger", return_value=self._fake_log()
        ):
            sync_project_results(str(self.project.identifier))
            sync_project_results(str(self.project.identifier))

        results = {
            result.file_path: result
            for result in TrainingResult.objects.filter(job=self.job)
        }
        self.assertEqual(len(results), 2)
        self.assertEqual(results[self.result_key].file_size, 123)
        self.assertEqual(results[checkpoint_key].file_size, 456)

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

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "results.services.get_s3_client", return_value=s3
        ):
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

    def test_result_services_include_non_global_checkpoints(self):
        """Per the Hub/CLI parity plan, valid model artifacts must not be
        filtered out just because they are not named ``FL_global_model.pt``.
        """
        legacy_key = (
            f"{self.project.identifier}/results/"
            f"{self.flare_uuid}/site-a/last_global_model.ckpt"
        )
        s3 = _FakeS3Client(
            [
                {
                    "Contents": [
                        {"Key": self.result_key, "Size": 123},
                        {"Key": legacy_key, "Size": 456},
                    ]
                }
            ]
        )

        with patch("results.services.get_s3_client", return_value=s3):
            rows = list_results(self.project, job_identifier=self.flare_uuid)

        keys = {row["file_path"] for row in rows}
        self.assertIn(self.result_key, keys)
        self.assertIn(legacy_key, keys)

        global_row = next(r for r in rows if r["file_path"] == self.result_key)
        legacy_row = next(r for r in rows if r["file_path"] == legacy_key)
        self.assertTrue(global_row["is_global_model"])
        self.assertFalse(legacy_row["is_global_model"])
        self.assertEqual(legacy_row["filename"], "last_global_model.ckpt")

    def test_result_services_tolerate_legacy_non_participant_layout(self):
        legacy_key = (
            f"{self.project.identifier}/results/"
            f"{self.flare_uuid}/model_weights.npz"
        )
        s3 = _FakeS3Client(
            [{"Contents": [{"Key": legacy_key, "Size": 789}]}]
        )

        with patch("results.services.get_s3_client", return_value=s3):
            rows = list_results(self.project, job_identifier=self.flare_uuid)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file_path"], legacy_key)
        self.assertEqual(rows[0]["client_name"], "local")
        self.assertTrue(rows[0]["is_global_model"])
        self.assertTrue(rows[0]["is_legacy_layout"])

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("results.services.get_s3_client", return_value=s3):
                payload = download_results_to_directory(
                    self.project,
                    output_dir=tmpdir,
                    job_identifier=self.flare_uuid,
                )

        downloaded = Path(payload["downloaded_files"][0])
        self.assertEqual(downloaded.name, "model_weights.npz")
        self.assertEqual(
            payload["global_model_md5s"],
            [hashlib.md5(b"weights").hexdigest()],  # nosec B324
        )

    def test_results_dashboard_context_uses_shared_layout_parser(self):
        legacy_key = (
            f"{self.project.identifier}/results/"
            f"{self.flare_uuid}/model_weights.npz"
        )
        s3 = _FakeS3Client(
            [
                {
                    "Contents": [
                        {"Key": self.result_key, "Size": 123},
                        {"Key": legacy_key, "Size": 789},
                    ]
                }
            ]
        )

        with patch("results.services.get_s3_client", return_value=s3):
            context = build_results_dashboard_context(
                self.project,
                selected_job_id=self.flare_uuid,
                default_to_latest=False,
                asynchronous_sync=False,
            )

        rows = {row["file_path"]: row for row in context["results"]}
        self.assertEqual(rows[self.result_key]["client_name"], "site-a")
        self.assertEqual(rows[legacy_key]["client_name"], "local")
        self.assertEqual(context["selected_job_id"], self.flare_uuid)
        self.assertTrue(context["has_jobs"])

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

    def test_resolve_results_plot_by_uuid_or_legacy_id(self):
        run = ResultsVisualizationRun.objects.create(
            project=self.project,
            job=self.job,
            flare_job_id=self.flare_uuid,
            user=self.user,
            status="completed",
        )
        plot = ResultsVisualizationPlot.objects.create(
            visualization_run=run,
            title="loss",
            plot_number=1,
            image_data="results-plot.png",
        )
        # UUID identifier path (what serialize_results_visualization_run emits)
        self.assertEqual(
            resolve_results_visualization_plot(self.project, str(plot.identifier)),
            plot,
        )
        # Legacy numeric id path (older URLs that captured the integer PK)
        self.assertEqual(
            resolve_results_visualization_plot(self.project, str(plot.id)),
            plot,
        )

    def test_status_json_plot_url_fetches_by_uuid(self):
        run = ResultsVisualizationRun.objects.create(
            project=self.project,
            job=self.job,
            flare_job_id=self.flare_uuid,
            user=self.user,
            status="completed",
        )
        plot = ResultsVisualizationPlot.objects.create(
            visualization_run=run,
            title="loss",
            plot_number=1,
            image_data="results-plot.png",
        )

        status_response = self.client.get(
            reverse(
                "results:results_visualization_status",
                args=[self.flare_uuid],
            )
        )
        self.assertEqual(status_response.status_code, 200)
        plot_url = status_response.json()["plots"][0]["image_url"]
        self.assertIn(str(plot.identifier), plot_url)

        with patch(
            "results.views._proxy_s3_download_file",
            return_value=HttpResponse(b"png", content_type="image/png"),
        ) as proxy:
            plot_response = self.client.get(plot_url)

        self.assertEqual(plot_response.status_code, 200)
        proxy.assert_called_once_with("results-plot.png", "plot_1.png")

    def test_resolve_results_plot_is_scoped_to_project(self):
        other_project = Project.objects.create(
            title="Other", author=self.user
        )
        other_run = ResultsVisualizationRun.objects.create(
            project=other_project,
            job=None,
            flare_job_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
            user=self.user,
            status="completed",
        )
        other_plot = ResultsVisualizationPlot.objects.create(
            visualization_run=other_run,
            title="loss",
            plot_number=1,
            image_data="other.png",
        )
        # A plot from a different project must not leak through.
        self.assertIsNone(
            resolve_results_visualization_plot(
                self.project, str(other_plot.identifier)
            )
        )
        self.assertIsNone(
            resolve_results_visualization_plot(self.project, str(other_plot.id))
        )

        other_url = reverse(
            "results:get_visualization_plot",
            args=[str(other_plot.identifier), "image"],
        )
        with patch("results.views._proxy_s3_download_file") as proxy:
            response = self.client.get(other_url)

        self.assertEqual(response.status_code, 404)
        proxy.assert_not_called()
