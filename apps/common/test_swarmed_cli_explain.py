"""Tests for the explainability commands of the swarmed CLI."""

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
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from swarmed_cli.main import build_parser, main

from network.models import SwarmNetwork
from project.models import Project, UserCurrentProject
from results.models import ExplanationRun
from results.test_explanations import login, make_bundle
from training.models import TrainingJob

FLARE_UUID = "99999999-9999-9999-9999-999999999999"


class ExplainParserTests(SimpleTestCase):
    def test_explain_command(self):
        args = build_parser().parse_args(
            ["results", "explain", "--job", "J", "--wait", "--timeout", "60"]
        )
        self.assertEqual(args.handler, "results_explain")
        self.assertEqual(args.command_name, "results explain")
        self.assertTrue(args.wait)
        self.assertEqual(args.timeout, 60.0)

    def test_explanations_command(self):
        args = build_parser().parse_args(
            ["results", "explanations", "--job", "J", "--out", "/tmp/x", "--reviews"]
        )
        self.assertEqual(args.handler, "results_explanations")
        self.assertEqual(args.out, "/tmp/x")
        self.assertTrue(args.reviews)
        self.assertIsNone(args.run)

    def test_explain_import_command(self):
        args = build_parser().parse_args(
            ["results", "explain-import", "--job", "J", "--dir", "bundle", "--json"]
        )
        self.assertEqual(args.handler, "results_explain_import")
        self.assertEqual(args.bundle_dir, "bundle")
        self.assertTrue(args.json)

    def test_explain_import_needs_a_folder(self):
        code = main(["results", "explain-import", "--job", "J", "--json"])
        self.assertEqual(code, 2)


class ExplainCommandTests(TestCase):
    def setUp(self):
        self.storage_dir = tempfile.mkdtemp(prefix="swm-cli-storage-")
        self.temp_dir = tempfile.mkdtemp(prefix="swm-cli-tmp-")
        self.work_dir = Path(tempfile.mkdtemp(prefix="swm-cli-work-"))
        for path in (self.storage_dir, self.temp_dir, self.work_dir):
            self.addCleanup(shutil.rmtree, path, True)
        override = self.settings(
            STORAGES={
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": self.storage_dir},
                },
                "staticfiles": {
                    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
                },
            },
            DEFAULT_FILE_STORAGE="django.core.files.storage.FileSystemStorage",
            MEDIA_ROOT=self.storage_dir,
            PROJECT_TEMP_DIR=self.temp_dir,
            ALLOWED_HOSTS=["testserver"],
            SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
        )
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user(username="cli-user", password="pw-1")
        self.stranger = User.objects.create_user(username="stranger", password="pw-2")
        self.project = Project.objects.create(title="CLI", author=self.user)
        UserCurrentProject.objects.create(user=self.user, project=self.project)
        network = SwarmNetwork.objects.create(
            name="CLI Network", project=self.project, author=self.user, status="RUNNING"
        )
        self.job = TrainingJob.objects.create(
            project=self.project,
            network=network,
            status="COMPLETED",
            flare_job_id=f"Submitted job: {FLARE_UUID}",
            flare_job_uuid=FLARE_UUID,
        )

    def _cli(self, *argv):
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(list(argv))
        output = stdout.getvalue()
        return code, (json.loads(output) if "--json" in argv else output)

    def _import(self, bundle, user="cli-user"):
        with patch("results.explanations.job_has_result_files", return_value=False):
            return self._cli(
                "results",
                "explain-import",
                "--user",
                user,
                "--project",
                str(self.project.identifier),
                "--job",
                FLARE_UUID,
                "--dir",
                str(bundle),
                "--json",
            )

    def test_import_list_export_round_trip(self):
        bundle = self.work_dir / "bundle"
        make_bundle(bundle, n_cases=3)
        code, envelope = self._import(bundle)
        self.assertEqual(code, 0, envelope)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["command"], "results explain-import")
        self.assertEqual(envelope["result"]["status"], "completed")
        self.assertEqual(envelope["result"]["source"], "import")
        self.assertEqual(envelope["result"]["cases"], 3)
        self.assertEqual(envelope["warnings"], [])  # the job has a training record

        out = self.work_dir / "export"
        code, envelope = self._cli(
            "results",
            "explanations",
            "--user",
            "cli-user",
            "--job",
            FLARE_UUID,
            "--out",
            str(out),
            "--json",
        )
        self.assertEqual(code, 0, envelope)
        result = envelope["result"]
        self.assertEqual(len(result["cases"]), 3)
        # The Hub stores the cases in a random order, so compare them as a set.
        self.assertEqual(
            sorted(case["case_id"] for case in result["cases"]),
            ["site-1:0000", "site-1:0001", "site-1:0002"],
        )
        for case in result["cases"]:
            self.assertEqual(case["panels"][0]["method"], "raw")
        self.assertEqual(result["output_dir"], str(out.resolve()))
        self.assertEqual(len(result["written_files"]), 1 + 3 * 2 + 1)
        self.assertTrue((out / "cases.json").is_file())

        code, envelope = self._import(out)
        self.assertEqual(code, 0, envelope)
        self.assertEqual(ExplanationRun.objects.filter(source="import").count(), 2)

    def test_import_warns_when_the_results_page_cannot_show_the_job(self):
        bundle = self.work_dir / "bundle"
        make_bundle(bundle)
        other = "12121212-1212-1212-1212-121212121212"
        with patch("results.explanations.job_has_result_files", return_value=False):
            code, envelope = self._cli(
                "results",
                "explain-import",
                "--user",
                "cli-user",
                "--job",
                other,
                "--dir",
                str(bundle),
                "--json",
            )
        self.assertEqual(code, 0, envelope)
        self.assertEqual(len(envelope["warnings"]), 1)
        self.assertIn("Results page", envelope["warnings"][0])

    def test_malicious_bundle_exits_2(self):
        bundle = self.work_dir / "bundle"
        make_bundle(bundle)
        secret = self.work_dir / "secret.txt"
        secret.write_text("TOKEN=abc\n")
        os.unlink(bundle / "case_01_panel_1.png")
        os.symlink(secret, bundle / "case_01_panel_1.png")
        code, envelope = self._import(bundle)
        self.assertEqual(code, 2)
        self.assertFalse(envelope["ok"])
        self.assertTrue(envelope["errors"])
        self.assertNotIn("TOKEN", envelope["errors"][0])
        self.assertFalse(ExplanationRun.objects.exists())

    def test_non_members_cannot_import(self):
        bundle = self.work_dir / "bundle"
        make_bundle(bundle)
        code, envelope = self._import(bundle, user="stranger")
        self.assertEqual(code, 2)
        self.assertFalse(ExplanationRun.objects.exists())

    def test_listing_without_explanations(self):
        code, envelope = self._cli(
            "results",
            "explanations",
            "--user",
            "cli-user",
            "--job",
            FLARE_UUID,
            "--json",
        )
        self.assertEqual(code, 0, envelope)
        self.assertIsNone(envelope["result"]["run"])
        self.assertEqual(envelope["result"]["cases"], [])
        code, envelope = self._cli(
            "results",
            "explanations",
            "--user",
            "cli-user",
            "--job",
            FLARE_UUID,
            "--out",
            str(self.work_dir / "none"),
            "--json",
        )
        self.assertEqual(code, 2)

    def test_explain_wait_returns_the_finished_run(self):
        finished = ExplanationRun.objects.create(
            project=self.project,
            job=self.job,
            flare_job_id=FLARE_UUID,
            user=self.user,
            status="completed",
            success=False,
            error_message="The script exited with code 1.",
        )
        with patch(
            "results.explanations.start_results_explanation", return_value=finished
        ) as start:
            code, envelope = self._cli(
                "results",
                "explain",
                "--user",
                "cli-user",
                "--job",
                FLARE_UUID,
                "--wait",
                "--interval",
                "0.01",
                "--json",
            )
        self.assertEqual(code, 0, envelope)
        start.assert_called_once_with(
            project=self.project, user=self.user, job_identifier=FLARE_UUID
        )
        self.assertEqual(envelope["result"]["status"], "completed")
        self.assertFalse(envelope["result"]["success"])

    def test_ui_and_cli_share_the_start_service(self):
        run = ExplanationRun.objects.create(
            project=self.project, job=self.job, flare_job_id=FLARE_UUID, user=self.user
        )
        with patch(
            "results.explanations.start_results_explanation", return_value=run
        ) as start:
            login(self.client, self.user)
            response = self.client.post(
                reverse("results:start_explanation", kwargs={"job_id": FLARE_UUID})
            )
            self.assertEqual(response.status_code, 200)
            code, _ = self._cli(
                "results", "explain", "--user", "cli-user", "--job", FLARE_UUID
            )
            self.assertEqual(code, 0)
        self.assertEqual(start.call_count, 2)

    def test_explain_without_script_exits_2(self):
        code, envelope = self._cli(
            "results", "explain", "--user", "cli-user", "--job", FLARE_UUID, "--json"
        )
        self.assertEqual(code, 2)
        self.assertIn("explainability script", envelope["errors"][0])
