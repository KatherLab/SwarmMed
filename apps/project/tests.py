"""Tests for project file ingestion."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils.datastructures import MultiValueDict

from . import services as project_services
from .models import Project
from .utils import handle_training_code_upload
from .views import project_create, project_edit


def _request_with_training_files(files, directories):
    return SimpleNamespace(
        POST={"training_code_directories": json.dumps(directories)},
        FILES=MultiValueDict({"training_code": files}),
    )


class TrainingCodeUploadTests(TestCase):
    """Regression coverage for Hub training-code uploads."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.storage_override = override_settings(
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                }
            },
            STORAGES={
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": self.tmpdir.name},
                },
                "staticfiles": {
                    "BACKEND": (
                        "django.contrib.staticfiles.storage.StaticFilesStorage"
                    ),
                },
            },
            DEFAULT_FILE_STORAGE="django.core.files.storage.FileSystemStorage",
            MEDIA_ROOT=self.tmpdir.name,
        )
        self.storage_override.enable()
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="project-user", password="test-password"
        )  # nosec B106
        self.project = Project.objects.create(title="Import Project", author=self.user)

    def tearDown(self):
        self.storage_override.disable()
        self.tmpdir.cleanup()

    def test_training_code_upload_sets_marker_to_training_py(self):
        request = _request_with_training_files(
            [
                SimpleUploadedFile("training.py", b"print('train')\n"),
                SimpleUploadedFile("helpers.py", b"def helper(): pass\n"),
            ],
            {
                "training.py_0": "training.py",
                "helpers.py_1": "package/helpers.py",
            },
        )

        handled = handle_training_code_upload(self.project, request)
        self.project.save()

        training_key = f"{self.project.identifier}/code/training/training.py"
        helper_key = f"{self.project.identifier}/code/training/package/helpers.py"
        self.assertTrue(handled)
        self.assertEqual(self.project.training_code.name, training_key)
        self.assertTrue(default_storage.exists(training_key))
        self.assertTrue(default_storage.exists(helper_key))

    def test_training_code_upload_without_training_py_is_refused(self):
        # Training runs custom/training.py, so code without a root-level
        # training.py is refused instead of storing another file as marker.
        request = _request_with_training_files(
            [SimpleUploadedFile("model.py", b"print('legacy')\n")],
            {"model.py_0": "model.py"},
        )

        with self.assertRaisesRegex(ValueError, "root-level training.py"):
            handle_training_code_upload(self.project, request)

        model_key = f"{self.project.identifier}/code/training/model.py"
        self.assertFalse(self.project.training_code)
        self.assertFalse(default_storage.exists(model_key))

    def test_training_code_upload_handles_multiple_files_without_directory_map(
        self,
    ):
        request = _request_with_training_files(
            [
                SimpleUploadedFile("training.py", b"print('train')\n"),
                SimpleUploadedFile("train_utils.py", b"VALUE = 1\n"),
            ],
            {},
        )

        handled = handle_training_code_upload(self.project, request)
        self.project.save()

        training_key = f"{self.project.identifier}/code/training/training.py"
        utils_key = f"{self.project.identifier}/code/training/train_utils.py"
        self.assertTrue(handled)
        self.assertEqual(self.project.training_code.name, training_key)
        self.assertTrue(default_storage.exists(training_key))
        self.assertTrue(default_storage.exists(utils_key))

    def test_training_code_upload_skips_non_python_files(self):
        request = _request_with_training_files(
            [
                SimpleUploadedFile("README.md", b"# ignored\n"),
                SimpleUploadedFile("training.py", b"print('train')\n"),
            ],
            {
                "README.md_0": "README.md",
                "training.py_1": "training.py",
            },
        )

        handled = handle_training_code_upload(self.project, request)
        self.project.save()

        readme_key = f"{self.project.identifier}/code/training/README.md"
        training_key = f"{self.project.identifier}/code/training/training.py"
        self.assertTrue(handled)
        self.assertEqual(self.project.training_code.name, training_key)
        self.assertFalse(default_storage.exists(readme_key))
        self.assertTrue(default_storage.exists(training_key))

    def test_training_code_upload_does_not_replace_marker_with_non_python_file(
        self,
    ):
        existing_key = f"{self.project.identifier}/code/training/existing.py"
        self.project.training_code.name = existing_key

        request = _request_with_training_files(
            [SimpleUploadedFile("README.md", b"# ignored\n")],
            {"README.md_0": "README.md"},
        )

        handled = handle_training_code_upload(self.project, request)

        self.assertTrue(handled)
        self.assertEqual(self.project.training_code.name, existing_key)
        self.assertFalse(
            default_storage.exists(f"{self.project.identifier}/code/training/README.md")
        )

    def test_project_create_view_persists_training_code_marker(self):
        request = self.factory.post(
            reverse("project:project_create"),
            {
                "title": "Created from Hub",
                "description": "Hub upload regression",
                "member_identifiers": "",
                "training_code_directories": json.dumps(
                    {
                        "training.py_0": "training.py",
                        "helper.py_1": "src/helper.py",
                    }
                ),
                "training_code": [
                    SimpleUploadedFile("training.py", b"print('train')\n"),
                    SimpleUploadedFile("helper.py", b"def helper(): pass\n"),
                ],
            },
        )
        request.user = self.user

        response = project_create(request)

        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(title="Created from Hub")
        training_key = f"{project.identifier}/code/training/training.py"
        helper_key = f"{project.identifier}/code/training/src/helper.py"
        self.assertEqual(project.training_code.name, training_key)
        self.assertTrue(default_storage.exists(training_key))
        self.assertTrue(default_storage.exists(helper_key))

    def _post_project_form(self, view, path, directories, files, **kwargs):
        request = self.factory.post(
            path,
            {
                "title": "Folder upload",
                "description": "",
                "member_identifiers": "",
                "training_code_directories": json.dumps(directories),
                "training_code": files,
            },
        )
        request.user = self.user
        return view(request, **kwargs)

    def test_select_project_folder_upload_is_stored_at_the_root(self):
        # Older pages send each file's path with the selected folder's own
        # name first; the shared folder is removed on the server.
        request = _request_with_training_files(
            [
                SimpleUploadedFile("training.py", b"print('train')\n"),
                SimpleUploadedFile("helper.py", b"x = 1\n"),
            ],
            {
                "training.py_0": "tabular_heart_disease/training.py",
                "helper.py_1": "tabular_heart_disease/utils/helper.py",
            },
        )

        handled = handle_training_code_upload(self.project, request)
        self.project.save()

        prefix = f"{self.project.identifier}/code/training/"
        self.assertTrue(handled)
        self.assertEqual(self.project.training_code.name, prefix + "training.py")
        self.assertTrue(default_storage.exists(prefix + "training.py"))
        self.assertTrue(default_storage.exists(prefix + "utils/helper.py"))
        self.assertFalse(default_storage.exists(prefix + "tabular_heart_disease"))

    def test_nested_training_py_is_refused_and_current_code_kept(self):
        prefix = f"{self.project.identifier}/code/training/"
        old_key = default_storage.save(
            prefix + "training.py", SimpleUploadedFile("training.py", b"print('old')\n")
        )
        self.project.training_code.name = old_key
        self.project.save()
        request = _request_with_training_files(
            [
                SimpleUploadedFile("training.py", b"print('new')\n"),
                SimpleUploadedFile("helper.py", b"x = 1\n"),
            ],
            {"training.py_0": "src/training.py", "helper.py_1": "helper.py"},
        )

        with self.assertRaisesRegex(ValueError, "root-level training.py"):
            handle_training_code_upload(self.project, request)

        self.project.refresh_from_db()
        self.assertEqual(self.project.training_code.name, old_key)
        with default_storage.open(old_key) as handle:
            self.assertEqual(handle.read(), b"print('old')\n")
        self.assertFalse(default_storage.exists(prefix + "src/training.py"))
        self.assertFalse(default_storage.exists(prefix + "helper.py"))

    def test_project_create_view_strips_selected_folder_name(self):
        response = self._post_project_form(
            project_create,
            reverse("project:project_create"),
            {
                "training.py_0": "my_task/training.py",
                "model.py_1": "my_task/lib/model.py",
            },
            [
                SimpleUploadedFile("training.py", b"print('train')\n"),
                SimpleUploadedFile("model.py", b"VALUE = 1\n"),
            ],
        )

        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(title="Folder upload")
        prefix = f"{project.identifier}/code/training/"
        self.assertEqual(project.training_code.name, prefix + "training.py")
        self.assertTrue(default_storage.exists(prefix + "training.py"))
        self.assertTrue(default_storage.exists(prefix + "lib/model.py"))

    def test_project_create_view_refuses_code_without_root_training_py(self):
        count = Project.objects.count()
        response = self._post_project_form(
            project_create,
            reverse("project:project_create"),
            {"training.py_0": "a/training.py", "model.py_1": "b/model.py"},
            [
                SimpleUploadedFile("training.py", b"print('train')\n"),
                SimpleUploadedFile("model.py", b"VALUE = 1\n"),
            ],
        )

        self.assertContains(
            response, "must contain a root-level training.py", status_code=200
        )
        self.assertEqual(Project.objects.count(), count)

    def test_project_edit_view_refuses_code_without_root_training_py(self):
        prefix = f"{self.project.identifier}/code/training/"
        old_key = default_storage.save(
            prefix + "training.py", SimpleUploadedFile("training.py", b"print('old')\n")
        )
        self.project.training_code.name = old_key
        self.project.save()

        response = self._post_project_form(
            project_edit,
            reverse("project:project_edit", args=[self.project.pk]),
            {"model.py_0": "model.py"},
            [SimpleUploadedFile("model.py", b"VALUE = 1\n")],
            pk=self.project.pk,
        )

        self.assertContains(
            response, "must contain a root-level training.py", status_code=200
        )
        self.project.refresh_from_db()
        self.assertEqual(self.project.title, "Import Project")
        self.assertEqual(self.project.training_code.name, old_key)
        self.assertTrue(default_storage.exists(old_key))
        self.assertFalse(default_storage.exists(prefix + "model.py"))

    def test_create_project_service_refuses_before_creating_the_project(self):
        count = Project.objects.count()
        request = _request_with_training_files(
            [SimpleUploadedFile("model.py", b"VALUE = 1\n")],
            {},
        )

        with self.assertRaisesRegex(ValueError, "root-level training.py"):
            project_services.create_project(
                author=self.user, title="Refused", request=request
            )

        self.assertEqual(Project.objects.count(), count)


class CodeDirectoryIngestTests(TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        storage = override_settings(
            STORAGES={
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": self.tmpdir.name + "/media"},
                },
                "staticfiles": {
                    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
                },
            },
            DEFAULT_FILE_STORAGE="django.core.files.storage.FileSystemStorage",
            MEDIA_ROOT=self.tmpdir.name + "/media",
        )
        storage.enable()
        self.addCleanup(storage.disable)
        user = User.objects.create_user(username="cli-user", password="pw")
        self.project = Project.objects.create(title="CLI code", author=user)

    def test_bytecode_caches_are_not_uploaded(self):
        code_dir = Path(self.tmpdir.name) / "template"
        (code_dir / "__pycache__").mkdir(parents=True)
        (code_dir / "lib" / "__pycache__").mkdir(parents=True)
        (code_dir / "training.py").write_text("print('train')\n")
        (code_dir / "lib" / "model.py").write_text("VALUE = 1\n")
        (code_dir / "__pycache__" / "training.cpython-312.pyc").write_bytes(b"\0")
        (code_dir / "lib" / "__pycache__" / "model.cpython-312.pyc").write_bytes(b"\0")
        (code_dir / "lib" / "stray.pyc").write_bytes(b"\0")

        project_services.ingest_code_directory(self.project, code_dir)

        stored = Path(self.tmpdir.name) / "media" / str(self.project.identifier)
        files = sorted(
            path.relative_to(stored).as_posix()
            for path in stored.rglob("*")
            if path.is_file()
        )
        self.assertEqual(
            files, ["code/training/lib/model.py", "code/training/training.py"]
        )
