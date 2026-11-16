"""CLI `results explanations --out`: unusable folders are refused before reading."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from swarmed_cli.main import main

from network.models import SwarmNetwork
from project.models import Project, UserCurrentProject
from results.models import ExplanationReader
from results.test_explanations import make_bundle
from training.models import TrainingJob

FLARE_UUID = "66666666-6666-6666-6666-666666666666"


class ExplanationsOutTests(TestCase):
    def setUp(self):
        storage_dir = tempfile.mkdtemp(prefix="swm-cli-out-storage-")
        temp_dir = tempfile.mkdtemp(prefix="swm-cli-out-tmp-")
        self.work_dir = Path(tempfile.mkdtemp(prefix="swm-cli-out-work-"))
        for path in (storage_dir, temp_dir, self.work_dir):
            self.addCleanup(shutil.rmtree, path, True)
        override = self.settings(
            STORAGES={
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": storage_dir},
                },
                "staticfiles": {
                    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
                },
            },
            DEFAULT_FILE_STORAGE="django.core.files.storage.FileSystemStorage",
            MEDIA_ROOT=storage_dir,
            PROJECT_TEMP_DIR=temp_dir,
        )
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user(username="cli-out", password="pw")
        project = Project.objects.create(title="CLI out", author=self.user)
        UserCurrentProject.objects.create(user=self.user, project=project)
        network = SwarmNetwork.objects.create(
            name="N", project=project, author=self.user, status="RUNNING"
        )
        TrainingJob.objects.create(
            project=project,
            network=network,
            status="COMPLETED",
            flare_job_id=f"Submitted job: {FLARE_UUID}",
            flare_job_uuid=FLARE_UUID,
        )
        bundle = self.work_dir / "bundle"
        make_bundle(bundle, n_cases=1)
        with patch("results.explanations.job_has_result_files", return_value=True):
            code, _ = self._cli(
                "results", "explain-import", "--job", FLARE_UUID, "--dir", str(bundle)
            )
        self.assertEqual(code, 0)

    def _cli(self, *argv):
        out = StringIO()
        with redirect_stdout(out):
            code = main([*argv, "--user", "cli-out", "--json"])
        return code, json.loads(out.getvalue())

    def _export(self, out):
        return self._cli("results", "explanations", "--job", FLARE_UUID, "--out", out)

    def _seen(self):
        return ExplanationReader.objects.filter(
            user=self.user, output_seen_at__isnull=False
        ).exists()

    def test_missing_parent_folder_is_refused_before_the_output_is_seen(self):
        missing = self.work_dir / "no" / "such" / "out"
        code, data = self._export(str(missing))
        self.assertEqual(code, 2)
        self.assertIn("parent folder", data["errors"][0])
        self.assertFalse((self.work_dir / "no").exists())
        self.assertFalse(self._seen())

    def test_non_empty_folder_is_refused_before_the_output_is_seen(self):
        busy = self.work_dir / "busy"
        busy.mkdir()
        (busy / "keep.txt").write_text("x")
        code, data = self._export(str(busy))
        self.assertEqual(code, 2)
        self.assertIn("is not empty", data["errors"][0])
        self.assertEqual(os.listdir(busy), ["keep.txt"])
        self.assertFalse(self._seen())

    def test_new_folder_receives_the_bundle(self):
        out = self.work_dir / "out"
        code, data = self._export(str(out))
        self.assertEqual(code, 0)
        self.assertIn("cases.json", os.listdir(out))
        self.assertEqual(data["result"]["output_dir"], str(out.resolve()))
        self.assertTrue(self._seen())
