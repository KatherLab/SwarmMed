"""Tests for the project's explainability script field."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Project, UserCurrentProject
from .services import (
    SPECIAL_CODE_DIR_FILES,
    SPECIAL_FIELD_FOLDERS,
    ingest_code_directory,
    remove_explainability_script_file,
)


def _fake_log():
    def no_op(*_args, **_kwargs):
        return None

    namespace = SimpleNamespace(info=no_op, debug=no_op, error=no_op, warning=no_op)
    return SimpleNamespace(auth=namespace, project=namespace, access=namespace)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
)
class ExplainabilityScriptTests(TestCase):
    def setUp(self):
        self.storage_dir = tempfile.mkdtemp(prefix="swm-project-storage-")
        self.code_dir = Path(tempfile.mkdtemp(prefix="swm-project-code-"))
        self.addCleanup(shutil.rmtree, self.storage_dir, True)
        self.addCleanup(shutil.rmtree, self.code_dir, True)
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
        )
        override.enable()
        self.addCleanup(override.disable)
        self.author = User.objects.create_user(username="author", password="pw-1")
        self.member = User.objects.create_user(username="member", password="pw-2")
        self.project = Project.objects.create(title="Code", author=self.author)
        self.project.members.add(self.member)

    def _login(self, user):
        user.profile.accepted_terms = True
        user.profile.accepted_policy = True
        user.profile.save()
        with patch("users.signals.get_logger", return_value=_fake_log()):
            self.client.force_login(user)

    def _write_code_dir(self, *, with_script=True):
        (self.code_dir / "training.py").write_text("print('train')\n")
        (self.code_dir / "sub").mkdir(exist_ok=True)
        (self.code_dir / "sub" / "explainability.py").write_text("# helper\n")
        script = self.code_dir / "explainability.py"
        if with_script:
            script.write_text("explain.set_info(target='x')\n")
        elif script.exists():
            script.unlink()

    def _stored(self):
        root = Path(self.storage_dir)
        return sorted(
            str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
        )

    def test_mapping_lists_the_explainability_script(self):
        self.assertEqual(
            SPECIAL_CODE_DIR_FILES["explainability.py"], "explainability_script"
        )
        self.assertEqual(
            SPECIAL_FIELD_FOLDERS["explainability_script"], "code/explainability/"
        )
        self.assertEqual(
            set(SPECIAL_CODE_DIR_FILES.values()), set(SPECIAL_FIELD_FOLDERS)
        )

    def test_code_dir_routes_the_root_script_to_its_field(self):
        self._write_code_dir()
        ingest_code_directory(self.project, self.code_dir)
        self.project.refresh_from_db()
        uuid = self.project.identifier
        self.assertEqual(
            self.project.explainability_script.name,
            f"{uuid}/code/explainability/explainability.py",
        )
        stored = self._stored()
        self.assertIn(f"{uuid}/code/training/sub/explainability.py", stored)
        self.assertNotIn(f"{uuid}/code/training/explainability.py", stored)

    def test_reingesting_without_the_script_clears_it(self):
        self._write_code_dir()
        ingest_code_directory(self.project, self.code_dir)
        self._write_code_dir(with_script=False)
        ingest_code_directory(self.project, self.code_dir)
        self.project.refresh_from_db()
        self.assertFalse(self.project.explainability_script)
        self.assertFalse(
            any("/code/explainability/" in name for name in self._stored())
        )

    def test_web_create_stores_the_uploaded_script(self):
        self._login(self.author)
        response = self.client.post(
            reverse("project:project_create"),
            {
                "title": "Uploaded",
                "description": "",
                "member_identifiers": "",
                "explainability_script": SimpleUploadedFile(
                    "explainability.py", b"explain.set_info(target='y')\n"
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(title="Uploaded")
        self.assertTrue(
            project.explainability_script.name.startswith(
                f"{project.identifier}/code/explainability/"
            )
        )
        with project.explainability_script.open("rb") as handle:
            self.assertEqual(handle.read(), b"explain.set_info(target='y')\n")

    def test_web_edit_replaces_and_removes_the_script(self):
        self.project.explainability_script.save(
            "explainability.py", ContentFile(b"old\n"), save=True
        )
        old_name = self.project.explainability_script.name
        self._login(self.author)
        url = reverse("project:project_edit", kwargs={"pk": self.project.pk})
        page = self.client.get(url)
        self.assertContains(page, "remove_explainability_script")
        self.assertContains(page, "Explainability Script")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                url,
                {
                    "title": "Code",
                    "description": "",
                    "member_identifiers": "",
                    "explainability_script": SimpleUploadedFile(
                        "explainability.py", b"new\n"
                    ),
                },
            )
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        with self.project.explainability_script.open("rb") as handle:
            self.assertEqual(handle.read(), b"new\n")
        stored = [name for name in self._stored() if "/code/explainability/" in name]
        self.assertEqual(stored, [self.project.explainability_script.name])
        if self.project.explainability_script.name != old_name:
            self.assertFalse(default_storage.exists(old_name))

        response = self.client.post(
            url,
            {
                "title": "Code",
                "description": "",
                "member_identifiers": "",
                "remove_explainability_script": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertFalse(self.project.explainability_script)
        self.assertFalse(
            any("/code/explainability/" in name for name in self._stored())
        )

    def test_members_cannot_change_the_script(self):
        self._login(self.member)
        url = reverse("project:project_edit", kwargs={"pk": self.project.pk})
        response = self.client.post(
            url,
            {
                "title": "Code",
                "explainability_script": SimpleUploadedFile("explainability.py", b"x"),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertFalse(self.project.explainability_script)

    def test_remove_service_clears_field_and_folder(self):
        self.project.explainability_script.save(
            "explainability.py", ContentFile(b"x\n"), save=True
        )
        remove_explainability_script_file(self.project)
        self.project.refresh_from_db()
        self.assertFalse(self.project.explainability_script)
        self.assertEqual(self._stored(), [])

    def test_project_list_lists_the_scripts_of_the_current_project(self):
        self.project.explainability_script.save(
            "explainability.py", ContentFile(b"x\n"), save=True
        )
        UserCurrentProject.objects.create(user=self.member, project=self.project)
        self._login(self.member)
        response = self.client.get(reverse("project:project_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse(
                "project:download_file",
                kwargs={"pk": self.project.pk, "file_type": "explainability_script"},
            ),
        )
