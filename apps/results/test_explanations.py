"""Tests for explanations in the Hub: models, bundle ingestion, task, views."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import DatabaseError, IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image, PngImagePlugin

from network.models import SwarmNetwork
from project.models import Project, UserCurrentProject
from training.models import TrainingJob

from . import explanations as explain_services
from .models import (
    ExplanationCase,
    ExplanationPanel,
    ExplanationReader,
    ExplanationReview,
    ExplanationRun,
    ExplanationSummary,
    explanation_image_path,
)
from .tasks import _tail_text, run_results_explanation_task

FLARE_UUID = "88888888-8888-8888-8888-888888888888"
SCRIPT = b"print('explaining')\n"


# --------------------------------------------------------------------------
# helpers (functions only, so importing them does not duplicate test classes)
# --------------------------------------------------------------------------


def png_bytes(size=(8, 8), color=(200, 30, 30), text=None):
    """Return a small PNG, optionally with a tEXt chunk."""
    info = None
    if text is not None:
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", text)
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG", pnginfo=info)
    return buffer.getvalue()


def make_bundle(
    path,
    *,
    n_cases=2,
    methods=("raw", "integrated_gradients"),
    summaries=1,
    outcomes=("TP", "FN"),
    label="abnormal",
    text=None,
):
    """Write a valid bundle with the package's own writer and return its data."""
    from swarmmed_explain.bundle import BundleWriter

    writer = BundleWriter(path)
    for index in range(n_cases):
        case_id = f"site-1:{index:04d}"
        writer.add_case(
            case_id,
            label=label,
            prediction="abnormal",
            score=round(0.9 - index * 0.1, 3),
            target="abnormal",
            outcome=outcomes[index % len(outcomes)],
            details={"age_group": "60-69", "lead": "II"},
        )
        for method in methods:
            writer.add_panel_png(
                case_id, method, png_bytes(text=text), title=f"{method} panel"
            )
    for index in range(summaries):
        writer.add_summary_png(f"Summary {index + 1}", png_bytes())
    writer.set_info(
        methods=list(methods),
        target="abnormal",
        notes="baseline: zeros",
        metrics={"auroc_test": 0.75},
    )
    return writer.finish()


def case_colour(index, panel):
    """Return the colour of panel ``panel`` of case ``index`` in a coloured bundle."""
    return (40 * index + 20, 30 * panel, 7)


def make_coloured_bundle(path, *, n_cases=4, panels=2):
    """Write a bundle whose panels have one colour per case and panel."""
    from swarmmed_explain.bundle import BundleWriter

    writer = BundleWriter(path)
    for index in range(n_cases):
        case_id = f"case-{index}"
        writer.add_case(case_id, label="abnormal", outcome=("TP", "FP")[index % 2])
        for panel in range(1, panels + 1):
            writer.add_panel_png(
                case_id,
                "raw" if panel == 1 else "occlusion",
                png_bytes(color=case_colour(index, panel)),
            )
    writer.add_summary_png("Summary", png_bytes())
    return writer.finish()


def stored_colour(field):
    """Return the colour of the top left pixel of a stored image."""
    with field.open("rb") as handle:
        image = Image.open(io.BytesIO(handle.read())).convert("RGB")
    return image.getpixel((0, 0))


def edit_cases_json(path, change, *, allow_nan=False):
    """Load cases.json of a bundle, apply ``change`` and write it back."""
    cases_path = Path(path) / "cases.json"
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    change(data)
    cases_path.write_text(json.dumps(data, allow_nan=allow_nan), encoding="utf-8")


def _fake_log():
    def no_op(*_args, **_kwargs):
        return None

    namespace = SimpleNamespace(info=no_op, debug=no_op, error=no_op, warning=no_op)
    return SimpleNamespace(auth=namespace, results=namespace, access=namespace)


def login(client, user):
    """Log a user in the way the other results tests do."""
    user.profile.accepted_terms = True
    user.profile.accepted_policy = True
    user.profile.save()
    with patch("users.signals.get_logger", return_value=_fake_log()):
        client.force_login(user)


# --------------------------------------------------------------------------
# base fixture
# --------------------------------------------------------------------------


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    AWS_STORAGE_BUCKET_NAME="test-bucket",
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
)
class ExplanationFixture(TestCase):
    """A project with an author, a member, an outsider and a training job."""

    def setUp(self):
        self.storage_dir = tempfile.mkdtemp(prefix="swm-explain-storage-")
        self.temp_dir = tempfile.mkdtemp(prefix="swm-explain-tmp-")
        self.work_dir = tempfile.mkdtemp(prefix="swm-explain-work-")
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
        )
        override.enable()
        self.addCleanup(override.disable)

        self.author = User.objects.create_user(username="author", password="pw-1")
        self.member = User.objects.create_user(username="member", password="pw-2")
        self.outsider = User.objects.create_user(username="outsider", password="pw-3")
        self.project = Project.objects.create(title="Explain", author=self.author)
        self.project.members.add(self.member)
        self.other_project = Project.objects.create(title="Other", author=self.outsider)
        for user, project in (
            (self.author, self.project),
            (self.member, self.project),
            (self.outsider, self.other_project),
        ):
            UserCurrentProject.objects.create(user=user, project=project)
        network = SwarmNetwork.objects.create(
            name="Explain Network",
            project=self.project,
            author=self.author,
            status="RUNNING",
        )
        self.job = TrainingJob.objects.create(
            project=self.project,
            network=network,
            status="COMPLETED",
            flare_job_id=f"Submitted job: {FLARE_UUID}",
            flare_job_uuid=FLARE_UUID,
        )

    def bundle_dir(self, name="bundle"):
        return os.path.join(self.work_dir, name)

    def import_bundle(self, user=None, **bundle_options):
        path = self.bundle_dir()
        make_bundle(path, **bundle_options)
        return explain_services.import_explanation_bundle(
            project=self.project,
            user=user or self.author,
            job_identifier=FLARE_UUID,
            bundle_dir=path,
        )

    def stored_files(self):
        root = Path(self.storage_dir)
        return sorted(
            str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
        )

    def add_script(self):
        self.project.explainability_script.save(
            "explainability.py", ContentFile(SCRIPT), save=True
        )


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


class ExplanationModelTests(ExplanationFixture):
    def test_import_creates_the_rows_of_the_bundle(self):
        run = self.import_bundle(n_cases=3, summaries=2)
        self.assertEqual(run.source, "import")
        self.assertEqual(run.status, "completed")
        self.assertTrue(run.success)
        cases = {case.case_id: case for case in run.cases.all()}
        self.assertEqual(sorted(case.position for case in cases.values()), [1, 2, 3])
        first = cases["site-1:0000"]
        self.assertEqual(first.outcome, "TP")
        self.assertEqual(cases["site-1:0001"].outcome, "FN")
        self.assertAlmostEqual(first.score, 0.9)
        self.assertEqual(first.details, {"age_group": "60-69", "lead": "II"})
        self.assertEqual(
            [panel.method for panel in first.panels.all()],
            ["raw", "integrated_gradients"],
        )
        self.assertEqual(run.summaries.count(), 2)
        self.assertEqual(run.info["metrics"], {"auroc_test": 0.75})
        panel = first.panels.first()
        self.assertEqual((panel.width, panel.height), (8, 8))
        self.assertTrue(panel.is_input)
        self.assertFalse(first.panels.last().is_input)

    def test_cases_are_stored_in_a_random_order_with_their_images(self):
        # Scripts add cases grouped by outcome; the stored order must not
        # follow the bundle order, and every image must stay with its case.
        path = self.bundle_dir()
        make_coloured_bundle(path, n_cases=4)
        with patch(
            "results.explanations._random_order",
            side_effect=lambda count: list(reversed(range(count))),
        ):
            run = explain_services.import_explanation_bundle(
                project=self.project,
                user=self.author,
                job_identifier=FLARE_UUID,
                bundle_dir=path,
            )
        cases = list(run.cases.all())
        self.assertEqual(
            [case.case_id for case in cases],
            ["case-3", "case-2", "case-1", "case-0"],
        )
        self.assertEqual([case.position for case in cases], [1, 2, 3, 4])
        prefix = f"{self.project.identifier}/plots/explanations/{run.identifier}/"
        for case in cases:
            index = int(case.case_id.split("-")[1])
            for panel in case.panels.all():
                with self.subTest(case=case.case_id, panel=panel.position):
                    self.assertEqual(
                        panel.image.name,
                        f"{prefix}case_{case.position:02d}_panel_{panel.position}.png",
                    )
                    self.assertEqual(
                        stored_colour(panel.image), case_colour(index, panel.position)
                    )
        # Export writes the stored order and the stored image names.
        data, images = explain_services.build_explanation_bundle(run)
        self.assertEqual(
            [item["case_id"] for item in data["cases"]],
            ["case-3", "case-2", "case-1", "case-0"],
        )
        self.assertEqual(data["cases"][0]["panels"][1]["file"], "case_01_panel_2.png")
        image = Image.open(io.BytesIO(images["case_01_panel_2.png"])).convert("RGB")
        self.assertEqual(image.getpixel((0, 0)), case_colour(3, 2))

    def test_random_order_comes_from_the_system_source(self):
        orders = [explain_services._random_order(8) for _ in range(5)]
        for order in orders:
            self.assertEqual(sorted(order), list(range(8)))
        # Five identity permutations in a row: probability 40320 ** -5.
        self.assertTrue(any(order != list(range(8)) for order in orders))
        with patch("results.explanations.secrets.SystemRandom") as source:
            explain_services._random_order(3)
        source.return_value.shuffle.assert_called_once_with([0, 1, 2])

    def test_sandbox_runs_are_stored_in_a_random_order_too(self):
        run = ExplanationRun.objects.create(
            project=self.project,
            job=self.job,
            flare_job_id=FLARE_UUID,
            user=self.author,
            status="running",
        )
        path = self.bundle_dir()
        make_coloured_bundle(path, n_cases=3)
        from swarmmed_explain.bundle import safe_copy_bundle

        staged = os.path.join(self.work_dir, "staged")
        collected = safe_copy_bundle(path, staged)
        with patch(
            "results.explanations._random_order", return_value=[1, 2, 0]
        ) as order:
            explain_services.persist_explanation_bundle(run, collected, staged)
        order.assert_called_once_with(3)
        cases = list(run.cases.all())
        self.assertEqual(
            [case.case_id for case in cases], ["case-1", "case-2", "case-0"]
        )
        self.assertEqual(
            stored_colour(cases[2].panels.get(position=1).image), case_colour(0, 1)
        )

    def test_images_are_stored_under_the_run_prefix(self):
        run = self.import_bundle()
        prefix = f"{self.project.identifier}/plots/explanations/{run.identifier}/"
        files = self.stored_files()
        self.assertEqual(len(files), 5)  # 2 cases x 2 panels + 1 summary
        self.assertTrue(all(name.startswith(prefix) for name in files))
        self.assertIn(prefix + "case_01_panel_1.png", files)
        self.assertIn(prefix + "summary_1.png", files)

    def test_string_representations_hold_no_case_data(self):
        run = self.import_bundle()
        case = run.cases.first()
        review = ExplanationReview.objects.create(
            case=case, reviewer=self.member, verdict="agree", note="secret note"
        )
        for obj in (run, case, case.panels.first(), run.summaries.first(), review):
            self.assertNotIn("site-1", str(obj))
            self.assertNotIn("secret", str(obj))

    def test_unique_case_position_and_id(self):
        run = self.import_bundle()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExplanationCase.objects.create(run=run, position=1, case_id="other")
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExplanationCase.objects.create(run=run, position=9, case_id="site-1:0000")

    def test_upload_path_uses_the_run(self):
        run = self.import_bundle()
        panel = ExplanationPanel.objects.filter(case__run=run).first()
        self.assertEqual(
            explanation_image_path(panel, "../../evil.png"),
            f"{self.project.identifier}/plots/explanations/{run.identifier}/evil.png",
        )

    def test_review_note_is_encrypted_at_rest(self):
        run = self.import_bundle()
        review = ExplanationReview.objects.create(
            case=run.cases.first(),
            reviewer=self.member,
            verdict="agree",
            note="plausible lead II",
        )
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT note FROM results_explanationreview WHERE id = %s",
                [review.id],
            )
            raw = cursor.fetchone()[0]
        self.assertNotIn("plausible", raw)
        review.refresh_from_db()
        self.assertEqual(review.note, "plausible lead II")

    def test_deleting_a_run_schedules_image_cleanup(self):
        run = self.import_bundle()
        prefix = f"{self.project.identifier}/plots/explanations/{run.identifier}/"
        with (
            patch("project.tasks.cleanup_project_files.delay") as cleanup,
            self.captureOnCommitCallbacks(execute=True),
        ):
            explain_services.delete_explanation_run(
                project=self.project, user=self.author, run_identifier=run.identifier
            )
        cleanup.assert_called_once_with(prefix)
        self.assertFalse(ExplanationRun.objects.filter(id=run.id).exists())
        self.assertFalse(ExplanationCase.objects.filter(run_id=run.id).exists())

    def test_deleting_a_case_deletes_its_images(self):
        run = self.import_bundle()
        case = run.cases.get(position=1)
        names = [panel.image.name for panel in case.panels.all()]
        summary = run.summaries.first().image.name
        self.assertEqual(len(names), 2)
        with (
            patch("project.tasks.cleanup_project_files.delay") as cleanup,
            self.captureOnCommitCallbacks(execute=True),
        ):
            case.delete()
        cleanup.assert_not_called()
        stored = self.stored_files()
        for name in names:
            self.assertNotIn(name, stored)
        self.assertIn(summary, stored)
        self.assertEqual(len(stored), 3)  # the other case's panels and the summary
        with self.captureOnCommitCallbacks(execute=True):
            run.summaries.all().delete()
        self.assertNotIn(summary, self.stored_files())

    def test_admin_does_not_delete_cases_reviews_or_images(self):
        from django.contrib import admin
        from django.test import RequestFactory

        superuser = User.objects.create_superuser("root", "root@example.org", "pw")
        request = RequestFactory().get("/admin/")
        request.user = superuser
        run = self.import_bundle()
        for model in (ExplanationRun, ExplanationCase, ExplanationReview):
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                self.assertFalse(model_admin.has_delete_permission(request))
                self.assertFalse(
                    model_admin.has_delete_permission(request, run.cases.first())
                )
                self.assertNotIn("delete_selected", model_admin.get_actions(request))
        for model in (ExplanationPanel, ExplanationSummary, ExplanationReader):
            with self.subTest(model=model.__name__):
                self.assertNotIn(model, admin.site._registry)


# --------------------------------------------------------------------------
# bundle ingestion (untrusted input)
# --------------------------------------------------------------------------


class BundleIngestionTests(ExplanationFixture):
    def _import_fails(self, path, message_part):
        with self.assertRaises(ValueError) as caught:
            explain_services.import_explanation_bundle(
                project=self.project,
                user=self.author,
                job_identifier=FLARE_UUID,
                bundle_dir=path,
            )
        self.assertIn(message_part, str(caught.exception))
        self.assertEqual(ExplanationRun.objects.count(), 0)
        self.assertEqual(ExplanationCase.objects.count(), 0)
        self.assertEqual(self.stored_files(), [])
        return caught.exception

    def test_png_text_chunks_are_dropped(self):
        run = self.import_bundle(text="patient Jane Doe")
        panel = ExplanationPanel.objects.filter(case__run=run).first()
        with panel.image.open("rb") as handle:
            data = handle.read()
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertNotIn(b"Jane", data)
        self.assertNotIn(b"tEXt", data)

    def test_symlink_to_a_file_outside_the_bundle_is_rejected(self):
        path = self.bundle_dir()
        make_bundle(path)
        secret = Path(self.work_dir) / "secret.env"
        secret.write_text("SECRET_KEY=do-not-leak\n")
        target = Path(path) / "case_01_panel_1.png"
        target.unlink()
        os.symlink(secret, target)
        error = self._import_fails(path, "case_01_panel_1.png")
        self.assertNotIn("do-not-leak", str(error))

    def test_fifo_does_not_block_and_is_rejected(self):
        path = self.bundle_dir()
        make_bundle(path)
        target = Path(path) / "summary_1.png"
        target.unlink()
        os.mkfifo(target)
        self._import_fails(path, "summary_1.png")

    def test_unknown_files_are_rejected(self):
        path = self.bundle_dir()
        make_bundle(path)
        (Path(path) / "notes.txt").write_text("x")
        self._import_fails(path, "notes.txt")

    def test_path_traversal_in_cases_json_is_rejected(self):
        for bad in ("../x.png", "/etc/passwd", "..\\x.png", "a/b.png"):
            path = self.bundle_dir("traversal")
            shutil.rmtree(path, ignore_errors=True)
            make_bundle(path)

            def change(data, bad=bad):
                data["cases"][0]["panels"][0]["file"] = bad

            edit_cases_json(path, change)
            with self.subTest(bad=bad):
                self._import_fails(path, "")

    def test_non_png_and_truncated_png_are_rejected(self):
        path = self.bundle_dir()
        make_bundle(path)
        (Path(path) / "case_01_panel_1.png").write_bytes(b"<svg onload=alert(1)>")
        self._import_fails(path, "case_01_panel_1.png")
        shutil.rmtree(path)
        make_bundle(path)
        whole = (Path(path) / "case_01_panel_2.png").read_bytes()
        (Path(path) / "case_01_panel_2.png").write_bytes(whole[:40])
        self._import_fails(path, "case_01_panel_2.png")

    def test_nan_score_is_rejected(self):
        path = self.bundle_dir()
        make_bundle(path)

        def change(data):
            data["cases"][0]["score"] = float("nan")

        edit_cases_json(path, change, allow_nan=True)
        self._import_fails(path, "")

    def test_markup_in_case_ids_and_identifier_keys_are_rejected(self):
        changes = [
            lambda data: data["cases"][0].update(case_id="<script>alert(1)</script>"),
            lambda data: data["cases"][0].update(case_id="x" * 129),
            lambda data: data["cases"][0].update(details={"patient_name": "Doe"}),
        ]
        for index, change in enumerate(changes):
            path = self.bundle_dir(f"bad-{index}")
            make_bundle(path)
            edit_cases_json(path, change)
            with self.subTest(index=index):
                self._import_fails(path, "")

    def test_too_many_cases_panels_and_summaries_are_rejected(self):
        from swarmmed_explain.bundle import panel_file_name

        def too_many_cases(data):
            first = data["cases"][0]
            data["cases"] = [
                dict(first, case_id=f"c{index}", panels=[]) for index in range(25)
            ]

        def too_many_panels(data):
            panel = data["cases"][0]["panels"][0]
            data["cases"][0]["panels"] = [
                dict(panel, file=panel_file_name(1, index % 4 + 1))
                for index in range(5)
            ]

        def too_many_summaries(data):
            summary = data["summaries"][0]
            data["summaries"] = [summary] * 5

        for index, change in enumerate(
            (too_many_cases, too_many_panels, too_many_summaries)
        ):
            path = self.bundle_dir(f"limits-{index}")
            make_bundle(path)
            edit_cases_json(path, change)
            with self.subTest(index=index):
                self._import_fails(path, "")

    def test_incomplete_bundle_is_rejected(self):
        path = self.bundle_dir()
        make_bundle(path)
        edit_cases_json(path, lambda data: data.update(complete=False))
        self._import_fails(path, "incomplete")

    def test_symlinked_bundle_folder_is_rejected(self):
        path = self.bundle_dir()
        make_bundle(path)
        link = os.path.join(self.work_dir, "link")
        os.symlink(path, link)
        self._import_fails(link, "")

    def test_import_needs_project_membership(self):
        path = self.bundle_dir()
        make_bundle(path)
        with self.assertRaises(PermissionError):
            explain_services.import_explanation_bundle(
                project=self.project,
                user=self.outsider,
                job_identifier=FLARE_UUID,
                bundle_dir=path,
            )

    def test_invalid_job_reference_is_rejected(self):
        path = self.bundle_dir()
        make_bundle(path)
        for bad in ("../x", "a/b", "", "<script>"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                explain_services.import_explanation_bundle(
                    project=self.project,
                    user=self.author,
                    job_identifier=bad,
                    bundle_dir=path,
                )

    def test_export_round_trips_through_import(self):
        run = self.import_bundle(n_cases=3)
        out = os.path.join(self.work_dir, "export")
        result = explain_services.export_explanations_to_directory(
            self.project, self.author, output_dir=out, job_identifier=FLARE_UUID
        )
        self.assertEqual(result["run_identifier"], str(run.identifier))
        self.assertEqual(len(result["written_files"]), 1 + 3 * 2 + 1)
        from swarmmed_explain.bundle import validate_bundle

        data = validate_bundle(out)
        self.assertEqual(
            [case["case_id"] for case in data["cases"]],
            [case.case_id for case in run.cases.all()],  # the stored order
        )
        again = explain_services.import_explanation_bundle(
            project=self.project,
            user=self.member,
            job_identifier=FLARE_UUID,
            bundle_dir=out,
        )
        self.assertEqual(again.cases.count(), 3)
        with self.assertRaises(ValueError):  # the folder is not empty any more
            explain_services.export_explanations_to_directory(
                self.project, self.author, output_dir=out, job_identifier=FLARE_UUID
            )


# --------------------------------------------------------------------------
# task (sandbox mocked)
# --------------------------------------------------------------------------


class ExplanationTaskTests(ExplanationFixture):
    def setUp(self):
        super().setUp()
        self.add_script()
        self.run_obj = ExplanationRun.objects.create(
            project=self.project,
            job=self.job,
            flare_job_id=FLARE_UUID,
            user=self.author,
        )
        self.seen = {}

    def _fake_sandbox(
        self, *, write_bundle=True, success=True, collect=True, output="ok"
    ):
        def fake(script_content, data_dir, project_uuid, run_type="validation", **kw):
            run_dir = tempfile.mkdtemp(dir=self.temp_dir)
            with open(os.path.join(run_dir, "script.py"), "w") as handle:
                handle.write(script_content)
            kw["prepare_run_dir"](run_dir)
            self.seen.update(
                run_type=run_type,
                container_name=kw.get("container_name"),
                files=sorted(os.listdir(run_dir)),
                package=sorted(os.listdir(os.path.join(run_dir, "swarmmed_explain"))),
                script=Path(run_dir, "explainability.py").read_bytes(),
                wrapper=script_content,
                manifest_dir=data_dir,
            )
            if write_bundle:
                make_bundle(os.path.join(run_dir, "explain"))
            result = {
                "success": success,
                "output": output,
                "exit_code": 0 if success else 1,
                "timed_out": False,
                "collected": None,
                "collect_error": None,
            }
            if success and collect:
                try:
                    result["collected"] = kw["collect_outputs"](run_dir)
                except Exception as exc:
                    result["collect_error"] = str(exc)
            if not success:
                result["error"] = "The script exited with code 1."
            shutil.rmtree(run_dir)
            return result

        return fake

    def _run_task(self, fake):
        with (
            patch("results.tasks.run_script_in_sandbox", side_effect=fake),
            patch("results.tasks._build_explanation_manifest", return_value={}),
        ):
            return run_results_explanation_task(self.run_obj.id, FLARE_UUID)

    def test_successful_run_stores_the_bundle(self):
        from swarmmed_explain.helper import wrapper_source

        outcome = self._run_task(self._fake_sandbox())
        self.assertEqual(outcome, {"success": True})
        self.run_obj.refresh_from_db()
        self.assertEqual(self.run_obj.status, "completed")
        self.assertTrue(self.run_obj.success)
        self.assertEqual(self.run_obj.output, "ok")
        self.assertEqual(len(self.run_obj.script_sha256), 64)
        self.assertEqual(self.run_obj.cases.count(), 2)
        self.assertEqual(self.run_obj.summaries.count(), 1)
        self.assertEqual(self.seen["run_type"], "explainability")
        self.assertEqual(
            self.seen["container_name"],
            f"swarmmedhub-explain-{self.run_obj.identifier}",
        )
        self.assertEqual(self.seen["wrapper"], wrapper_source())
        self.assertEqual(self.seen["script"], SCRIPT)
        self.assertIn("explainability.py", self.seen["files"])
        self.assertIn("swarmmed_explain", self.seen["files"])
        self.assertIn("helper.py", self.seen["package"])
        self.assertNotIn("tests", self.seen["package"])
        # staging folders are removed
        self.assertEqual(os.listdir(self.temp_dir), [])

    def test_failed_script_stores_nothing(self):
        self._run_task(self._fake_sandbox(success=False, output="Traceback ..."))
        self.run_obj.refresh_from_db()
        self.assertEqual(self.run_obj.status, "completed")
        self.assertFalse(self.run_obj.success)
        self.assertIn("exited with code 1", self.run_obj.error_message)
        self.assertEqual(self.run_obj.output, "Traceback ...")
        self.assertEqual(self.run_obj.cases.count(), 0)
        self.assertEqual(
            [name for name in self.stored_files() if "/plots/" in name], []
        )

    def test_rejected_bundle_stores_nothing(self):
        def fake(*args, **kw):
            result = self._fake_sandbox(write_bundle=False)(*args, **kw)
            return result

        self._run_task(fake)
        self.run_obj.refresh_from_db()
        self.assertFalse(self.run_obj.success)
        self.assertIn("rejected", self.run_obj.error_message)
        self.assertEqual(ExplanationCase.objects.count(), 0)

    def test_cancelled_run_is_not_overwritten(self):
        inner = self._fake_sandbox()

        def fake(*args, **kw):
            result = inner(*args, **kw)
            ExplanationRun.objects.filter(id=self.run_obj.id).update(status="cancelled")
            return result

        outcome = self._run_task(fake)
        self.assertEqual(outcome["status"], "cancelled")
        self.run_obj.refresh_from_db()
        self.assertEqual(self.run_obj.status, "cancelled")
        self.assertEqual(self.run_obj.cases.count(), 0)

    def test_run_cancelled_before_start_is_skipped(self):
        ExplanationRun.objects.filter(id=self.run_obj.id).update(status="cancelled")
        with patch("results.tasks.run_script_in_sandbox") as sandbox:
            outcome = run_results_explanation_task(self.run_obj.id, FLARE_UUID)
        sandbox.assert_not_called()
        self.assertFalse(outcome["success"])

    def test_missing_script_fails_the_run(self):
        self.project.explainability_script.delete(save=True)
        with patch("results.tasks.run_script_in_sandbox") as sandbox:
            run_results_explanation_task(self.run_obj.id, FLARE_UUID)
        sandbox.assert_not_called()
        self.run_obj.refresh_from_db()
        self.assertEqual(self.run_obj.status, "failed")
        self.assertIn("no explainability script", self.run_obj.error_message)

    def test_output_keeps_the_last_64_kib(self):
        long_output = "x" * (70 * 1024) + "END"
        self._run_task(self._fake_sandbox(output=long_output))
        self.run_obj.refresh_from_db()
        self.assertTrue(self.run_obj.output.endswith("END"))
        self.assertLessEqual(len(self.run_obj.output.encode()), 64 * 1024 + 64)
        self.assertEqual(_tail_text("short"), "short")

    def test_nul_characters_in_the_output_are_replaced(self):
        self.assertEqual(_tail_text("a\x00b"), "a�b")
        self.assertEqual(
            _tail_text("x" * 10 + "\x00", limit=5), "[earlier output omitted]\nxx�"
        )
        outcome = self._run_task(self._fake_sandbox(output="progress\x00done\n"))
        self.assertEqual(outcome, {"success": True})
        self.run_obj.refresh_from_db()
        self.assertEqual(self.run_obj.status, "completed")
        self.assertTrue(self.run_obj.success)
        self.assertEqual(self.run_obj.output, "progress�done\n")
        self.assertEqual(self.run_obj.cases.count(), 2)

    def test_a_failure_while_storing_leaves_no_cases_or_images(self):
        def broken_bulk_create(*args, **kwargs):
            raise RuntimeError("database went away")

        with patch.object(
            ExplanationSummary.objects, "bulk_create", side_effect=broken_bulk_create
        ):
            outcome = self._run_task(self._fake_sandbox(output="ok\n"))
        self.assertEqual(outcome, {"success": False})
        self.run_obj.refresh_from_db()
        self.assertEqual(self.run_obj.status, "failed")
        self.assertFalse(self.run_obj.success)
        self.assertIn("database went away", self.run_obj.error_message)
        self.assertEqual(self.run_obj.output, "ok\n")
        self.assertEqual(self.run_obj.cases.count(), 0)
        self.assertEqual(ExplanationPanel.objects.count(), 0)
        self.assertEqual(
            [name for name in self.stored_files() if "/plots/" in name], []
        )

    def test_a_failure_after_storing_removes_the_stored_images(self):
        # The run's final save comes before the bundle is stored, so the only
        # failure after storing is the commit; the images must not stay.
        real_atomic = transaction.atomic
        stored_before_commit = []

        @contextlib.contextmanager
        def atomic_failing_at_commit(*args, **kwargs):
            with real_atomic(*args, **kwargs):
                yield
                stored_before_commit.extend(
                    name for name in self.stored_files() if "/plots/" in name
                )
                raise DatabaseError("could not commit")

        with patch(
            "results.tasks.transaction",
            SimpleNamespace(atomic=atomic_failing_at_commit),
        ):
            outcome = self._run_task(self._fake_sandbox(output="ok\n"))
        self.assertEqual(len(stored_before_commit), 5)
        self.assertEqual(outcome, {"success": False})
        self.run_obj.refresh_from_db()
        self.assertEqual(self.run_obj.status, "failed")
        self.assertIn("could not commit", self.run_obj.error_message)
        self.assertEqual(self.run_obj.output, "ok\n")
        self.assertEqual(self.run_obj.cases.count(), 0)
        self.assertEqual(
            [name for name in self.stored_files() if "/plots/" in name], []
        )

    def test_a_task_never_overwrites_a_cancellation(self):
        # Cancelled between the task's start and the stored result: nothing
        # is stored, also if the cancellation comes right before the lock.
        inner = self._fake_sandbox()

        def fake(*args, **kw):
            result = inner(*args, **kw)
            explain_services._mark_cancelled(self.run_obj)
            return result

        outcome = self._run_task(fake)
        self.assertEqual(outcome, {"success": False, "status": "cancelled"})
        self.run_obj.refresh_from_db()
        self.assertEqual(self.run_obj.status, "cancelled")
        self.assertIsNone(self.run_obj.success)
        self.assertEqual(self.run_obj.output, "")
        self.assertEqual(self.run_obj.cases.count(), 0)
        self.assertEqual(os.listdir(self.temp_dir), [])

    def test_failure_of_a_cancelled_run_keeps_it_cancelled(self):
        def fake(*args, **kw):
            explain_services._mark_cancelled(self.run_obj)
            raise RuntimeError("docker went away")

        outcome = self._run_task(fake)
        self.assertEqual(outcome, {"success": False})
        self.run_obj.refresh_from_db()
        self.assertEqual(self.run_obj.status, "cancelled")
        self.assertEqual(self.run_obj.error_message, "")


class _FakeSandboxContainer:
    """A sandbox container, as far as the sandbox code uses one.

    As with Docker, ``wait`` returns (exit code 137) once the container has
    been removed by name; ``on_wait`` stands for a stop request that arrives
    while the worker waits.
    """

    def __init__(self, name, on_wait):
        self.name = name
        self.on_wait = on_wait
        self.removed = False

    def wait(self, timeout=None):
        self.on_wait()
        if not self.removed:
            raise AssertionError("the container is still running")
        return {"StatusCode": 137}

    def logs(self, **_kwargs):
        return b"case site-1:0000 TP score=0.9\n"

    def kill(self):
        return None

    def remove(self, force=False):
        import docker

        if self.removed:
            raise docker.errors.NotFound("No such container")
        self.removed = True


class _FakeSandboxClient:
    """The sandbox daemon's client: ``containers.run`` and ``containers.get``."""

    def __init__(self, on_wait):
        self.on_wait = on_wait
        self.started = []
        self.looked_up = []
        self.containers = self

    def run(self, **kwargs):
        container = _FakeSandboxContainer(kwargs.get("name"), self.on_wait)
        self.started.append(container)
        return container

    def get(self, name):
        import docker

        self.looked_up.append(name)
        for container in self.started:
            if container.name == name and not container.removed:
                return container
        raise docker.errors.NotFound("No such container")


@patch.dict(os.environ, {"SWARMMEDHUB_ENABLE_GPU": "false"})
class ExplanationStopTests(ExplanationFixture):
    """Stopping a run while the real sandbox code runs it (Docker is faked)."""

    def setUp(self):
        super().setUp()
        self.add_script()
        self.run_obj = ExplanationRun.objects.create(
            project=self.project,
            job=self.job,
            flare_job_id=FLARE_UUID,
            user=self.member,
            celery_task_id="task-7",
        )
        self.container_name = f"swarmmedhub-explain-{self.run_obj.identifier}"

    def _stop(self):
        explain_services.stop_results_explanation(
            project=self.project,
            user=self.member,
            run_identifier=self.run_obj.identifier,
        )

    def _run_task(self, client, manifest=None):
        with (
            patch(
                "results.tasks._build_explanation_manifest",
                side_effect=manifest or (lambda *args: {}),
            ),
            patch("data.sandbox.get_docker_client", return_value=client),
            patch("data.sandbox.ensure_sandbox_image"),
            patch("data.sandbox.ensure_sandbox_network"),
            patch(
                "data.sandbox._resolve_minio_host_for_sandbox",
                side_effect=OSError("no MinIO here"),
            ),
            patch("data.sandbox.get_host_path", side_effect=lambda path: path),
            patch("results.explanations.current_app.control.revoke") as revoke,
        ):
            outcome = run_results_explanation_task(self.run_obj.id, FLARE_UUID)
        return outcome, revoke

    def test_stopping_a_running_run_cleans_up_without_killing_the_worker(self):
        client = _FakeSandboxClient(on_wait=self._stop)
        outcome, revoke = self._run_task(client)
        self.assertEqual(outcome, {"success": False, "status": "cancelled"})
        # No terminate=True: that would SIGTERM the worker process, which then
        # dies without running the clean-up checked below.
        revoke.assert_called_once_with("task-7")
        self.assertEqual([item.name for item in client.started], [self.container_name])
        self.assertTrue(client.started[0].removed)
        self.assertIn(self.container_name, client.looked_up)
        self.run_obj.refresh_from_db()
        self.assertEqual(self.run_obj.status, "cancelled")
        self.assertIsNone(self.run_obj.success)
        self.assertEqual(self.run_obj.output, "")
        self.assertEqual(self.run_obj.error_message, "")
        self.assertEqual(self.run_obj.cases.count(), 0)
        # The run folder (script and package), the data folder (manifest with
        # presigned URLs) and the staging folder are gone.
        self.assertEqual(os.listdir(self.temp_dir), [])

    def test_a_run_stopped_before_its_container_starts_does_not_start(self):
        client = _FakeSandboxClient(on_wait=lambda: None)

        def manifest(*args):
            self._stop()  # the stop arrives while the data is prepared
            return {}

        outcome, revoke = self._run_task(client, manifest=manifest)
        self.assertEqual(outcome, {"success": False, "status": "cancelled"})
        revoke.assert_called_once_with("task-7")
        self.assertEqual(client.started, [])
        self.assertEqual(client.looked_up, [self.container_name])
        self.run_obj.refresh_from_db()
        self.assertEqual(self.run_obj.status, "cancelled")
        self.assertEqual(os.listdir(self.temp_dir), [])


# --------------------------------------------------------------------------
# services: start, stop, delete
# --------------------------------------------------------------------------


@patch("results.explanations.remove_sandbox_container")
@patch("results.explanations.current_app.control.revoke")
class ExplanationRunServiceTests(ExplanationFixture):
    def _start(self, user):
        with patch(
            "results.explanations.run_results_explanation_task.delay",
            return_value=SimpleNamespace(id="task-1"),
        ) as delay:
            run = explain_services.start_results_explanation(
                project=self.project, user=user, job_identifier=str(self.job.identifier)
            )
        return run, delay

    def test_start_needs_a_script(self, _revoke, _remove):
        with self.assertRaises(ValueError):
            self._start(self.member)

    def test_start_queues_the_task_and_cancels_older_runs(self, revoke, remove):
        self.add_script()
        first, delay = self._start(self.member)
        delay.assert_called_once_with(first.id, FLARE_UUID)
        self.assertEqual(first.celery_task_id, "task-1")
        self.assertEqual(first.job, self.job)
        second, _ = self._start(self.author)  # the author may stop any run
        first.refresh_from_db()
        self.assertEqual(first.status, "cancelled")
        self.assertIsNotNone(first.completed_at)
        # Revoked without terminate: SIGTERM would kill the worker process
        # before its clean-up runs.
        revoke.assert_called_once_with("task-1")
        remove.assert_called_once_with(f"swarmmedhub-explain-{first.identifier}")
        self.assertEqual(second.status, "pending")
        third, _ = self._start(self.author)  # the user's own run
        second.refresh_from_db()
        self.assertEqual(second.status, "cancelled")
        self.assertEqual(third.status, "pending")

    def test_start_does_not_cancel_another_members_run(self, revoke, remove):
        self.add_script()
        other = User.objects.create_user(username="member2", password="pw-9")
        self.project.members.add(other)
        first, _ = self._start(self.member)
        with self.assertRaises(ValueError) as raised:
            self._start(other)
        self.assertIn("started by member", str(raised.exception))
        first.refresh_from_db()
        self.assertEqual(first.status, "pending")
        revoke.assert_not_called()
        remove.assert_not_called()
        self.assertEqual(
            ExplanationRun.objects.filter(status__in=("pending", "running")).count(),
            1,
        )
        # Once the run has finished, the other member can start one.
        ExplanationRun.objects.filter(pk=first.pk).update(
            status="completed", success=False
        )
        run, _ = self._start(other)
        self.assertEqual(run.status, "pending")

    def test_start_view_reports_another_members_run(self, _revoke, _remove):
        self.add_script()
        other = User.objects.create_user(username="member2", password="pw-9")
        self.project.members.add(other)
        UserCurrentProject.objects.create(user=other, project=self.project)
        self._start(self.member)
        client = Client()
        login(client, other)
        response = client.post(
            reverse("results:start_explanation", kwargs={"job_id": FLARE_UUID})
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("started by member", response.json()["error"])

    def test_stop_of_a_finished_run_keeps_its_result(self, revoke, remove):
        self.add_script()
        run, _ = self._start(self.member)
        # The task finished between the page's status poll and the stop.
        ExplanationRun.objects.filter(pk=run.pk).update(
            status="completed", success=True
        )
        with self.assertRaises(ValueError):
            explain_services.stop_results_explanation(
                project=self.project, user=self.member, run_identifier=run.identifier
            )
        # The cancellation applies only to an active run, also when the
        # caller's copy of the run is out of date.
        self.assertEqual(run.status, "pending")
        self.assertFalse(explain_services._mark_cancelled(run))
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")
        self.assertTrue(run.success)
        revoke.assert_not_called()
        remove.assert_not_called()

    def test_start_needs_membership(self, _revoke, _remove):
        self.add_script()
        with self.assertRaises(PermissionError):
            self._start(self.outsider)

    def test_stop_by_creator_or_author_only(self, _revoke, remove):
        self.add_script()
        run, _ = self._start(self.member)
        other = User.objects.create_user(username="member2", password="pw-4")
        self.project.members.add(other)
        with self.assertRaises(PermissionError):
            explain_services.stop_results_explanation(
                project=self.project, user=other, run_identifier=run.identifier
            )
        explain_services.stop_results_explanation(
            project=self.project, user=self.author, run_identifier=run.identifier
        )
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")
        with self.assertRaises(ValueError):
            explain_services.stop_results_explanation(
                project=self.project, user=self.author, run_identifier=run.identifier
            )

    def test_runs_of_another_project_are_not_found(self, _revoke, _remove):
        run = self.import_bundle()
        for bad in (run.identifier, "not-a-uuid", "123"):
            with self.subTest(bad=bad), self.assertRaises(LookupError):
                explain_services.resolve_explanation_run(self.other_project, bad)

    def test_delete_rules(self, _revoke, _remove):
        run = self.import_bundle(user=self.member)
        other = User.objects.create_user(username="member3", password="pw-5")
        self.project.members.add(other)
        with self.assertRaises(PermissionError):
            explain_services.delete_explanation_run(
                project=self.project, user=other, run_identifier=run.identifier
            )
        explain_services.delete_explanation_run(
            project=self.project, user=self.member, run_identifier=run.identifier
        )
        self.assertFalse(ExplanationRun.objects.exists())

    def test_only_the_author_deletes_a_run_with_other_users_reviews(
        self, _revoke, _remove
    ):
        run = self.import_bundle(user=self.member)
        case = run.cases.first()
        for reviewer in (self.member, self.author):
            explain_services.add_explanation_review(
                project=self.project,
                user=reviewer,
                case_identifier=case.identifier,
                verdict="agree",
            )
        with self.assertRaises(PermissionError) as raised:
            explain_services.delete_explanation_run(
                project=self.project, user=self.member, run_identifier=run.identifier
            )
        self.assertIn("only the project author", str(raised.exception))
        self.assertEqual(ExplanationReview.objects.filter(case=case).count(), 2)
        explain_services.delete_explanation_run(
            project=self.project, user=self.author, run_identifier=run.identifier
        )
        self.assertFalse(ExplanationRun.objects.exists())
        self.assertFalse(ExplanationReview.objects.exists())


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------


class ExplanationViewTests(ExplanationFixture):
    def setUp(self):
        super().setUp()
        self.run_obj = self.import_bundle(n_cases=2)
        self.case = self.run_obj.cases.first()
        self.map_panel = self.case.panels.get(method="integrated_gradients")
        self.raw_panel = self.case.panels.get(method="raw")

    def client_for(self, user, **kwargs):
        client = Client(**kwargs)
        login(client, user)
        return client

    def url(self, name, **kwargs):
        return reverse(f"results:{name}", kwargs=kwargs)

    def test_status_and_case_list_for_a_member(self):
        client = self.client_for(self.member)
        response = client.get(self.url("explanation_status", job_id=FLARE_UUID))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["runs"][0]["identifier"], str(self.run_obj.identifier))
        # Without blinded mode (switched off: a first-time reviewer starts
        # blinded) the case list has the whole output.
        response = client.post(
            self.url("explanation_mode", run_uuid=str(self.run_obj.identifier)),
            {"blinded": "0"},
        )
        self.assertEqual(response.status_code, 200)
        response = client.get(
            self.url("explanation_cases", run_uuid=str(self.run_obj.identifier))
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["blinded"])
        self.assertEqual(len(data["cases"]), 2)
        first = data["cases"][0]
        self.assertEqual(first["case_id"], self.case.case_id)
        self.assertEqual(first["prediction"], "abnormal")
        self.assertEqual(first["target"], "abnormal")
        self.assertEqual(first["details"], {"age_group": "60-69", "lead": "II"})
        self.assertEqual(len(first["panels"]), 2)
        self.assertEqual(len(data["summaries"]), 1)
        self.assertTrue(
            first["panels"][0]["image_url"].startswith("/results/explain/image/panel/")
        )

    def test_image_is_served_as_png_with_safe_headers(self):
        explain_services.set_reader_mode(self.run_obj, self.member, blinded=False)
        client = self.client_for(self.member)
        response = client.get(
            self.url(
                "explanation_image",
                image_kind="panel",
                image_uuid=str(self.map_panel.identifier),
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assertTrue(response.content.startswith(b"\x89PNG"))
        response = client.get(
            self.url("explanation_image", image_kind="svg", image_uuid="x")
        )
        self.assertEqual(response.status_code, 404)

    def test_non_members_are_refused(self):
        client = self.client_for(self.outsider)
        # The outsider's current project is their own: lookups are scoped to it.
        for url in (
            self.url("explanation_cases", run_uuid=str(self.run_obj.identifier)),
            self.url(
                "explanation_image",
                image_kind="panel",
                image_uuid=str(self.map_panel.identifier),
            ),
            self.url("explanation_reviews", case_uuid=str(self.case.identifier)),
        ):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 404)
        response = client.post(
            self.url("explanation_reviews", case_uuid=str(self.case.identifier)),
            {"verdict": "agree"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ExplanationReview.objects.count(), 0)
        # Pointing the current project at a project they do not belong to
        # sends them back to the project list.
        UserCurrentProject.objects.filter(user=self.outsider).update(
            project=self.project
        )
        response = client.get(self.url("explanation_status", job_id=FLARE_UUID))
        self.assertEqual(response.status_code, 302)
        # (the decorator dropped the relation; point it at the project again)
        UserCurrentProject.objects.update_or_create(
            user=self.outsider, defaults={"project": self.project}
        )
        response = client.get(
            self.url("explanation_cases", run_uuid=str(self.run_obj.identifier))
        )
        self.assertEqual(response.status_code, 302)

    def test_anonymous_users_are_redirected_to_login(self):
        response = Client().get(self.url("explanation_status", job_id=FLARE_UUID))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_review_post_is_append_only_and_uses_the_logged_in_user(self):
        client = self.client_for(self.member)
        url = self.url("explanation_reviews", case_uuid=str(self.case.identifier))
        response = client.post(
            url,
            {"verdict": "disagree", "note": "map on noise", "reviewer": self.author.id},
        )
        self.assertEqual(response.status_code, 201)
        response = client.post(url, {"verdict": "agree", "note": "second look"})
        self.assertEqual(response.status_code, 201)
        reviews = list(ExplanationReview.objects.order_by("id"))
        self.assertEqual(len(reviews), 2)
        self.assertTrue(all(review.reviewer == self.member for review in reviews))
        self.assertEqual(reviews[0].verdict, "disagree")  # unchanged
        data = response.json()
        self.assertEqual(len(data["case"]["reviews"]), 1)  # latest per reviewer
        self.assertEqual(data["case"]["reviews"][0]["verdict"], "agree")
        history = client.get(url).json()["reviews"]
        self.assertEqual([item["verdict"] for item in history], ["agree", "disagree"])

    def test_review_validation(self):
        client = self.client_for(self.member)
        url = self.url("explanation_reviews", case_uuid=str(self.case.identifier))
        for payload in (
            {"verdict": "maybe"},
            {"verdict": "agree", "note": "x" * 2001},
            {"verdict": "agree", "note": "bad\x00byte"},
        ):
            with self.subTest(payload=list(payload)):
                self.assertEqual(client.post(url, payload).status_code, 400)
        self.assertEqual(
            client.post(url, {"verdict": "agree", "note": "y" * 2000}).status_code, 201
        )

    def test_review_post_needs_the_csrf_token(self):
        client = self.client_for(self.member, enforce_csrf_checks=True)
        url = self.url("explanation_reviews", case_uuid=str(self.case.identifier))
        self.assertEqual(client.post(url, {"verdict": "agree"}).status_code, 403)
        self.assertEqual(ExplanationReview.objects.count(), 0)

    def test_break_glass_users_can_read_but_not_review(self):
        auditor = User.objects.create_user(username="auditor", password="pw-6")
        auditor.profile.is_emergency_access = True
        auditor.profile.save()
        UserCurrentProject.objects.create(user=auditor, project=self.project)
        client = self.client_for(auditor)
        response = client.get(
            self.url("explanation_cases", run_uuid=str(self.run_obj.identifier))
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["can_review"])
        response = client.post(
            self.url("explanation_reviews", case_uuid=str(self.case.identifier)),
            {"verdict": "agree"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ExplanationReview.objects.count(), 0)

    def test_blinded_mode_hides_output_until_a_read_is_saved(self):
        client = self.client_for(self.member)
        run_uuid = str(self.run_obj.identifier)
        ExplanationReview.objects.create(
            case=self.case, reviewer=self.author, verdict="agree", note="author read"
        )
        data = client.post(
            self.url("explanation_mode", run_uuid=run_uuid), {"blinded": "1"}
        ).json()
        self.assertTrue(data["blinded"])
        self.assertTrue(data["reads_blinded"])
        first = data["cases"][0]
        self.assertTrue(first["hidden"])
        for key in ("prediction", "score", "outcome", "target"):
            self.assertIsNone(first[key])
        self.assertEqual(first["details"], {})
        self.assertEqual([panel["method"] for panel in first["panels"]], ["raw"])
        self.assertEqual(first["hidden_panels"], 1)
        self.assertEqual(first["reviews"], [])  # the author's read stays hidden
        self.assertEqual(data["summaries"], [])
        self.assertEqual(data["hidden_summaries"], 1)
        self.assertEqual(first["label"], "abnormal")

        image_url = self.url(
            "explanation_image",
            image_kind="panel",
            image_uuid=str(self.map_panel.identifier),
        )
        self.assertEqual(client.get(image_url).status_code, 404)
        raw_url = self.url(
            "explanation_image",
            image_kind="panel",
            image_uuid=str(self.raw_panel.identifier),
        )
        self.assertEqual(client.get(raw_url).status_code, 200)

        response = client.post(
            self.url("explanation_reviews", case_uuid=str(self.case.identifier)),
            {"verdict": "agree", "note": "abnormal ST segment", "blinded": "0"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["review"]["blinded"])
        revealed = response.json()["case"]
        self.assertFalse(revealed["hidden"])
        self.assertEqual(revealed["prediction"], "abnormal")
        self.assertEqual(revealed["target"], "abnormal")
        self.assertEqual(revealed["details"], {"age_group": "60-69", "lead": "II"})
        self.assertEqual(len(revealed["panels"]), 2)
        self.assertEqual(len(revealed["reviews"]), 2)
        self.assertEqual(client.get(image_url).status_code, 200)

        # A second review of the same case is made after seeing the output.
        response = client.post(
            self.url("explanation_reviews", case_uuid=str(self.case.identifier)),
            {"verdict": "disagree"},
        )
        self.assertFalse(response.json()["review"]["blinded"])
        # Summaries stay hidden until every case was read.
        data = client.get(self.url("explanation_cases", run_uuid=run_uuid)).json()
        self.assertEqual(data["summaries"], [])
        last = self.run_obj.cases.last()
        client.post(
            self.url("explanation_reviews", case_uuid=str(last.identifier)),
            {"verdict": "unsure"},
        )
        data = client.get(self.url("explanation_cases", run_uuid=run_uuid)).json()
        self.assertEqual(len(data["summaries"]), 1)

    def test_reads_after_seeing_the_output_are_not_blinded(self):
        client = self.client_for(self.member)
        run_uuid = str(self.run_obj.identifier)
        mode_url = self.url("explanation_mode", run_uuid=run_uuid)
        client.post(mode_url, {"blinded": "0"})  # the output is shown
        reader = ExplanationReader.objects.get(run=self.run_obj, user=self.member)
        self.assertIsNotNone(reader.output_seen_at)
        data = client.post(mode_url, {"blinded": "1"}).json()
        self.assertTrue(data["blinded"])
        self.assertFalse(data["reads_blinded"])
        # One rule for showing and recording: the run counts as seen, so every
        # case is shown (the page asks about the output, not the label) ...
        for item in data["cases"]:
            self.assertFalse(item["hidden"])
            self.assertEqual(item["prediction"], "abnormal")
            self.assertEqual(len(item["panels"]), 2)
        self.assertEqual(len(data["summaries"]), 1)
        # ... and a review is recorded as made after seeing the output.
        response = client.post(
            self.url("explanation_reviews", case_uuid=str(self.case.identifier)),
            {"verdict": "agree"},
        )
        self.assertFalse(response.json()["review"]["blinded"])
        self.assertFalse(response.json()["case"]["hidden"])
        image_url = self.url(
            "explanation_image",
            image_kind="panel",
            image_uuid=str(self.run_obj.cases.last().panels.last().identifier),
        )
        self.assertEqual(client.get(image_url).status_code, 200)

    def test_a_download_or_cli_listing_reveals_the_run_while_blinded(self):
        run_uuid = str(self.run_obj.identifier)
        for user, give_output in (
            (
                self.member,
                lambda client: b"".join(
                    client.post(
                        self.url("explanation_download", run_uuid=run_uuid)
                    ).streaming_content
                ),
            ),
            (
                self.author,
                lambda client: explain_services.describe_explanations(
                    self.project, self.author, job_identifier=FLARE_UUID
                ),
            ),
        ):
            with self.subTest(user=user.username):
                client = self.client_for(user)
                data = client.get(
                    self.url("explanation_cases", run_uuid=run_uuid)
                ).json()
                self.assertTrue(data["reads_blinded"])
                self.assertTrue(data["cases"][0]["hidden"])
                give_output(client)
                data = client.get(
                    self.url("explanation_cases", run_uuid=run_uuid)
                ).json()
                self.assertTrue(data["blinded"])
                self.assertFalse(data["reads_blinded"])
                self.assertFalse(any(item["hidden"] for item in data["cases"]))
                response = client.post(
                    self.url(
                        "explanation_reviews", case_uuid=str(self.case.identifier)
                    ),
                    {"verdict": "disagree"},
                )
                self.assertFalse(response.json()["review"]["blinded"])

    def test_blinded_choice_carries_over_to_new_runs(self):
        client = self.client_for(self.member)
        client.post(
            self.url("explanation_mode", run_uuid=str(self.run_obj.identifier)),
            {"blinded": "1"},
        )
        newer = self.import_bundle_again()
        data = client.get(
            self.url("explanation_cases", run_uuid=str(newer.identifier))
        ).json()
        self.assertTrue(data["blinded"])
        self.assertTrue(data["cases"][0]["hidden"])

    def import_bundle_again(self, name="second"):
        path = self.bundle_dir(name)
        make_bundle(path)
        return explain_services.import_explanation_bundle(
            project=self.project,
            user=self.author,
            job_identifier=FLARE_UUID,
            bundle_dir=path,
        )

    def test_a_first_time_reviewer_starts_blinded(self):
        # The requests the results page makes on load: status, then the
        # newest run's cases. Nothing of the model output may be in them.
        client = self.client_for(self.member)
        status = client.get(self.url("explanation_status", job_id=FLARE_UUID)).json()
        run_uuid = status["runs"][0]["identifier"]
        data = client.get(self.url("explanation_cases", run_uuid=run_uuid)).json()
        self.assertTrue(data["blinded"])
        self.assertTrue(data["reads_blinded"])
        for item in data["cases"]:
            self.assertTrue(item["hidden"])
            self.assertIsNone(item["prediction"])
            self.assertIsNone(item["target"])
            self.assertEqual(item["details"], {})
        self.assertEqual(data["summaries"], [])
        reader = ExplanationReader.objects.get(run=self.run_obj, user=self.member)
        self.assertIsNone(reader.output_seen_at)
        response = client.post(
            self.url("explanation_reviews", case_uuid=str(self.case.identifier)),
            {"verdict": "agree"},
        )
        self.assertTrue(response.json()["review"]["blinded"])

    def test_the_unblinded_choice_carries_over_to_new_runs(self):
        client = self.client_for(self.member)
        client.post(
            self.url("explanation_mode", run_uuid=str(self.run_obj.identifier)),
            {"blinded": "0"},
        )
        newer = self.import_bundle_again()
        data = client.get(
            self.url("explanation_cases", run_uuid=str(newer.identifier))
        ).json()
        self.assertFalse(data["blinded"])
        self.assertFalse(data["cases"][0]["hidden"])

    def test_new_runs_start_in_the_last_chosen_mode(self):
        # Seeing an older run's output (here a download) is not a choice of
        # mode: a new run starts in the mode the reviewer chose last.
        client = self.client_for(self.member)
        older_uuid = str(self.run_obj.identifier)
        client.post(self.url("explanation_mode", run_uuid=older_uuid), {"blinded": "0"})
        second = self.import_bundle_again("second")
        client.post(
            self.url("explanation_mode", run_uuid=str(second.identifier)),
            {"blinded": "1"},
        )
        b"".join(
            client.post(
                self.url("explanation_download", run_uuid=older_uuid)
            ).streaming_content
        )
        third = self.import_bundle_again("third")
        data = client.get(
            self.url("explanation_cases", run_uuid=str(third.identifier))
        ).json()
        self.assertTrue(data["blinded"])
        self.assertTrue(data["reads_blinded"])

    def test_script_output_stays_hidden_while_reading_blinded(self):
        ExplanationRun.objects.filter(pk=self.run_obj.pk).update(
            output="site-1:0000 FP score=0.912\n"
        )
        client = self.client_for(self.member)
        run_uuid = str(self.run_obj.identifier)
        status_url = self.url("explanation_status", job_id=FLARE_UUID)
        cases_url = self.url("explanation_cases", run_uuid=run_uuid)
        data = client.get(cases_url).json()
        self.assertTrue(data["reads_blinded"])
        self.assertEqual(data["run"]["output"], "")
        self.assertTrue(data["run"]["output_hidden"])
        status = client.get(status_url).json()
        self.assertEqual(status["latest"]["output"], "")
        self.assertNotIn("score=0.912", json.dumps(status))
        # Once every case is read, the output appears in the run's payload.
        for case in self.run_obj.cases.all():
            client.post(
                self.url("explanation_reviews", case_uuid=str(case.identifier)),
                {"verdict": "unsure"},
            )
        data = client.get(cases_url).json()
        self.assertIn("score=0.912", data["run"]["output"])
        self.assertFalse(data["run"]["output_hidden"])
        self.assertIsNone(
            ExplanationReader.objects.get(
                run=self.run_obj, user=self.member
            ).output_seen_at
        )
        # A failed run keeps its output and error in the status.
        failed = ExplanationRun.objects.create(
            project=self.project,
            job=self.job,
            flare_job_id=FLARE_UUID,
            user=self.member,
            status="failed",
            success=False,
            output="Traceback: KeyError 'x'",
            error_message="The script exited with code 1.",
        )
        status = client.get(status_url).json()
        self.assertEqual(status["latest"]["identifier"], str(failed.identifier))
        self.assertEqual(status["latest"]["output"], "Traceback: KeyError 'x'")
        self.assertEqual(
            status["latest"]["error_message"], "The script exited with code 1."
        )
        data = client.get(
            self.url("explanation_cases", run_uuid=str(failed.identifier))
        ).json()
        self.assertEqual(data["run"]["output"], "Traceback: KeyError 'x'")

    def test_cli_listing_leaves_out_the_output_of_other_runs(self):
        ExplanationRun.objects.filter(pk=self.run_obj.pk).update(output="older run")
        newer = self.import_bundle_again()
        ExplanationRun.objects.filter(pk=newer.pk).update(output="newer run")
        result = explain_services.describe_explanations(
            self.project, self.member, job_identifier=FLARE_UUID
        )
        self.assertEqual(result["run"]["identifier"], str(newer.identifier))
        self.assertEqual(result["run"]["output"], "newer run")
        outputs = {item["identifier"]: item["output"] for item in result["runs"]}
        self.assertEqual(outputs[str(newer.identifier)], "newer run")
        self.assertEqual(outputs[str(self.run_obj.identifier)], "")

    def test_a_stored_runs_output_comes_only_with_its_cases(self):
        # What `swarmed results explain --wait` and the start and stop views
        # return: a run with stored explanations without its output, a run
        # that stored nothing with it.
        ExplanationRun.objects.filter(pk=self.run_obj.pk).update(
            output="site-1:0000 FP score=0.912\n"
        )
        self.run_obj.refresh_from_db()
        data = explain_services.serialize_explanation_run(self.run_obj)
        self.assertEqual(data["output"], "")
        self.assertTrue(data["output_hidden"])
        failed = ExplanationRun.objects.create(
            project=self.project,
            job=self.job,
            flare_job_id=FLARE_UUID,
            user=self.member,
            status="completed",
            success=False,
            output="Traceback: KeyError 'x'",
        )
        data = explain_services.serialize_explanation_run(failed)
        self.assertEqual(data["output"], "Traceback: KeyError 'x'")
        self.assertFalse(data["output_hidden"])
        self.assertIsNone(
            ExplanationReader.objects.filter(
                run=self.run_obj, user=self.member, output_seen_at__isnull=False
            ).first()
        )

    def test_images_follow_the_carried_over_mode_and_record_seen_output(self):
        client = self.client_for(self.member)
        map_url = self.url(
            "explanation_image",
            image_kind="panel",
            image_uuid=str(self.map_panel.identifier),
        )
        raw_url = self.url(
            "explanation_image",
            image_kind="panel",
            image_uuid=str(self.raw_panel.identifier),
        )
        summary_url = self.url(
            "explanation_image",
            image_kind="summary",
            image_uuid=str(self.run_obj.summaries.first().identifier),
        )
        # A first-time reviewer opening an image by URL before the case list:
        # the reader row is created in blinded mode and the map is refused.
        self.assertEqual(client.get(map_url).status_code, 404)
        self.assertEqual(client.get(summary_url).status_code, 404)
        self.assertEqual(client.get(raw_url).status_code, 200)
        reader = ExplanationReader.objects.get(run=self.run_obj, user=self.member)
        self.assertTrue(reader.blinded)
        self.assertIsNone(reader.output_seen_at)
        # A reviewer who does not read blinded: an input panel is not model
        # output; a map panel is, and the run then counts as seen.
        author = self.client_for(self.author)
        explain_services.set_reader_mode(self.run_obj, self.author, blinded=False)
        self.assertEqual(author.get(raw_url).status_code, 200)
        reader = ExplanationReader.objects.get(run=self.run_obj, user=self.author)
        self.assertIsNone(reader.output_seen_at)
        self.assertEqual(author.get(map_url).status_code, 200)
        reader.refresh_from_db()
        self.assertIsNotNone(reader.output_seen_at)
        explain_services.set_reader_mode(self.run_obj, self.author, blinded=True)
        data = author.get(
            self.url("explanation_cases", run_uuid=str(self.run_obj.identifier))
        ).json()
        self.assertFalse(data["reads_blinded"])
        self.assertFalse(data["cases"][0]["hidden"])

    def test_page_says_what_blinded_review_hides(self):
        client = self.client_for(self.member)
        empty_s3 = SimpleNamespace(
            get_paginator=lambda _name: SimpleNamespace(paginate=lambda **_kw: [])
        )
        with (
            patch("results.services.get_s3_client", return_value=empty_s3),
            patch("results.services.sync_results"),
        ):
            html = client.get(reverse("results:results")).content.decode()
        for text in (
            "Prediction, score, outcome, target and details hidden until you "
            "save a read",
            "addField(fields, 'Target', hiddenText);",
            "addField(fields, 'Details', hiddenText);",
            "so all its cases are shown and reviews you save are recorded as made "
            "after seeing the output.",
            "The cases are in a random order.",
        ):
            self.assertIn(text, html)

    def test_bundle_download_is_a_zip_of_the_bundle(self):
        client = self.client_for(self.member)
        url = self.url("explanation_download", run_uuid=str(self.run_obj.identifier))
        self.assertEqual(client.get(url).status_code, 405)
        response = client.post(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        content = b"".join(response.streaming_content)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = sorted(archive.namelist())
            out = os.path.join(self.work_dir, "unzipped")
            archive.extractall(out)
        self.assertIn("cases.json", names)
        self.assertEqual(len(names), 6)
        from swarmmed_explain.bundle import validate_bundle

        self.assertEqual(len(validate_bundle(out)["cases"]), 2)
        reader = ExplanationReader.objects.get(run=self.run_obj, user=self.member)
        self.assertIsNotNone(reader.output_seen_at)

    def test_start_stop_and_delete_views(self):
        client = self.client_for(self.member)
        response = client.post(self.url("start_explanation", job_id=FLARE_UUID))
        self.assertEqual(response.status_code, 400)  # no script yet
        self.assertIn("explainability script", response.json()["error"])
        self.add_script()
        with (
            patch(
                "results.explanations.run_results_explanation_task.delay",
                return_value=SimpleNamespace(id="task-9"),
            ),
            patch("results.explanations.remove_sandbox_container"),
            patch("results.explanations.current_app.control.revoke"),
        ):
            response = client.post(self.url("start_explanation", job_id=FLARE_UUID))
            self.assertEqual(response.status_code, 200)
            run_id = response.json()["run_id"]
            self.assertEqual(
                client.get(
                    self.url("start_explanation", job_id=FLARE_UUID)
                ).status_code,
                405,
            )
            response = client.post(self.url("stop_explanation"), {"run_id": run_id})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["run"]["status"], "cancelled")
        response = client.post(self.url("delete_explanation"), {"run_id": run_id})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ExplanationRun.objects.filter(identifier=run_id).exists())
        other_client = self.client_for(self.outsider)
        response = other_client.post(
            self.url("delete_explanation"), {"run_id": str(self.run_obj.identifier)}
        )
        self.assertEqual(response.status_code, 404)

    def test_results_page_renders_the_section_safely(self):
        client = self.client_for(self.member)
        empty_s3 = SimpleNamespace(
            get_paginator=lambda _name: SimpleNamespace(paginate=lambda **_kw: [])
        )
        with (
            patch("results.services.get_s3_client", return_value=empty_s3),
            patch("results.services.sync_results"),
        ):
            response = client.get(
                reverse("results:results"), {"job": '"><script>alert(1)</script>'}
            )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="explainability"', html)
        self.assertIn('id="explain-config"', html)
        self.assertNotIn("<script>alert(1)</script>", html)
        start = html.rindex("<script>", 0, html.index("Explainability section."))
        script = html[start : html.index("</script>", start)]
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("insertAdjacentHTML", script)
        config = json.loads(
            html.split('id="explain-config" type="application/json">')[1].split(
                "</script>"
            )[0]
        )
        self.assertEqual(config["job_id"], "")  # the bad job value is dropped
        self.assertIsNone(config["status_url"])

    def test_case_data_in_json_is_not_interpreted(self):
        path = self.bundle_dir("markup")
        make_bundle(path, label="<img src=x onerror=alert(1)>")
        run = explain_services.import_explanation_bundle(
            project=self.project,
            user=self.author,
            job_identifier=FLARE_UUID,
            bundle_dir=path,
        )
        client = self.client_for(self.member)
        response = client.get(
            self.url("explanation_cases", run_uuid=str(run.identifier))
        )
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(
            response.json()["cases"][0]["label"], "<img src=x onerror=alert(1)>"
        )


# --------------------------------------------------------------------------
# storage helpers
# --------------------------------------------------------------------------


class StoredScriptTests(ExplanationFixture):
    def test_has_explainability_script_checks_storage(self):
        self.assertFalse(explain_services.has_explainability_script(self.project))
        self.add_script()
        self.assertTrue(explain_services.has_explainability_script(self.project))
        default_storage.delete(self.project.explainability_script.name)
        self.assertFalse(explain_services.has_explainability_script(self.project))

    def test_summary_visibility_follows_reads(self):
        run = self.import_bundle()
        summary = ExplanationSummary.objects.get(run=run)
        explain_services.set_reader_mode(run, self.member, blinded=True)
        with self.assertRaises(LookupError):
            explain_services.resolve_explanation_image(
                self.project, "summary", summary.identifier, user=self.member
            )
        for case in run.cases.all():
            explain_services.add_explanation_review(
                project=self.project,
                user=self.member,
                case_identifier=case.identifier,
                verdict="unsure",
            )
        self.assertEqual(
            explain_services.resolve_explanation_image(
                self.project, "summary", summary.identifier, user=self.member
            ),
            summary,
        )


class ExplanationExportTests(ExplanationFixture):
    def test_personal_data_export_lists_runs_and_reviews(self):
        run = self.import_bundle(user=self.member)
        explain_services.add_explanation_review(
            project=self.project,
            user=self.member,
            case_identifier=run.cases.first().identifier,
            verdict="disagree",
            note="map on noise",
        )
        client = Client()
        login(client, self.member)
        response = client.get(reverse("users:export_data"))
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(
            [item["identifier"] for item in data["explanation_runs"]],
            [str(run.identifier)],
        )
        self.assertEqual(len(data["explanation_reviews"]), 1)
        review = data["explanation_reviews"][0]
        self.assertEqual(review["note"], "map on noise")
        self.assertEqual(review["case_position"], 1)
        self.assertNotIn("site-1", response.content.decode())
