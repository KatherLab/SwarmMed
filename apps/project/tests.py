"""Tests for project file ingestion."""

from __future__ import annotations

import json
import tempfile
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils.datastructures import MultiValueDict

from .models import Project
from .utils import handle_training_code_upload
from .views import project_create


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
        self.project = Project.objects.create(
            title="Upload Project", author=self.user
        )

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

    def test_training_code_upload_uses_first_python_file_as_legacy_marker(self):
        request = _request_with_training_files(
            [SimpleUploadedFile("model.py", b"print('legacy')\n")],
            {"model.py_0": "model.py"},
        )

        handled = handle_training_code_upload(self.project, request)
        self.project.save()

        model_key = f"{self.project.identifier}/code/training/model.py"
        self.assertTrue(handled)
        self.assertEqual(self.project.training_code.name, model_key)
        self.assertTrue(default_storage.exists(model_key))

    def test_training_code_upload_handles_multiple_files_without_directory_map(
        self,
    ):
        request = _request_with_training_files(
            [
                SimpleUploadedFile("model.py", b"print('model')\n"),
                SimpleUploadedFile("train_utils.py", b"VALUE = 1\n"),
            ],
            {},
        )

        handled = handle_training_code_upload(self.project, request)
        self.project.save()

        model_key = f"{self.project.identifier}/code/training/model.py"
        utils_key = (
            f"{self.project.identifier}/code/training/train_utils.py"
        )
        self.assertTrue(handled)
        self.assertEqual(self.project.training_code.name, model_key)
        self.assertTrue(default_storage.exists(model_key))
        self.assertTrue(default_storage.exists(utils_key))

    def test_training_code_upload_skips_non_python_files(self):
        request = _request_with_training_files(
            [
                SimpleUploadedFile("README.md", b"# ignored\n"),
                SimpleUploadedFile("training.py", b"print('train')\n"),
            ],
            {
                "README.md_0": "README.md",
                "training.py_1": "package/training.py",
            },
        )

        handled = handle_training_code_upload(self.project, request)
        self.project.save()

        readme_key = f"{self.project.identifier}/code/training/README.md"
        training_key = (
            f"{self.project.identifier}/code/training/package/training.py"
        )
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
            default_storage.exists(
                f"{self.project.identifier}/code/training/README.md"
            )
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
