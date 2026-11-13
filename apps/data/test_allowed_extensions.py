"""The default import allowlist (ALLOWED_EXTENSIONS) and the import filter using it."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, override_settings

from data import services

REPO = Path(__file__).resolve().parents[2]

# Raw formats of the templates' data: audio (WAV, FLAC), EDF and BDF recordings,
# WFDB records (.hea header, .dat signal), MATLAB files and tab-separated tables.
TEMPLATE_FORMATS = (".wav", ".flac", ".edf", ".bdf", ".dat", ".hea", ".mat", ".tsv")
EARLIER_FORMATS = (
    ".csv",
    ".txt",
    ".json",
    ".parquet",
    ".npy",
    ".npz",
    ".h5",
    ".pt",
    ".pth",
    ".dcm",
    ".nii",
    ".nii.gz",
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".gif",
    ".pdf",
)


def _as_set(value: str) -> set[str]:
    """Parse a comma-separated list the way core/settings/base.py does."""
    return {ext.strip().lower() for ext in value.split(",") if ext.strip()}


def _settings_default() -> str:
    """The default of os.environ.get("ALLOWED_EXTENSIONS", ...) in the settings."""
    tree = ast.parse((REPO / "core" / "settings" / "base.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "ALLOWED_EXTENSIONS"
            and isinstance(node.args[1], ast.Constant)
        ):
            return node.args[1].value
    raise AssertionError("core/settings/base.py has no default for ALLOWED_EXTENSIONS")


def _setup_env_example() -> str:
    """The value scripts/setup_env.py offers for ALLOWED_EXTENSIONS."""
    tree = ast.parse((REPO / "scripts" / "setup_env.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "ALLOWED_EXTENSIONS"
                and isinstance(value, ast.Dict)
            ):
                fields = {
                    k.value: v.value
                    for k, v in zip(value.keys, value.values, strict=True)
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
                }
                return fields["example"]
    raise AssertionError("scripts/setup_env.py offers no ALLOWED_EXTENSIONS value")


class DefaultAllowlistTests(SimpleTestCase):
    def test_default_accepts_the_templates_raw_formats(self):
        default = _as_set(_settings_default())
        for extension in TEMPLATE_FORMATS + EARLIER_FORMATS:
            with self.subTest(extension=extension):
                self.assertIn(extension, default)

    def test_setup_env_offers_the_settings_default(self):
        self.assertEqual(_as_set(_setup_env_example()), _as_set(_settings_default()))


class ImportFilterTests(SimpleTestCase):
    def test_import_keeps_allowed_files_and_skips_others(self):
        project = SimpleNamespace(identifier=uuid.uuid4())
        entries = [
            ("fl-client-1/a0001.wav", ContentFile(b"RIFF")),
            ("fl-client-1/a0001.flac", ContentFile(b"fLaC")),
            ("records100/00001_lr.hea", ContentFile(b"00001_lr 12 100 1000")),
            ("records100/00001_lr.dat", ContentFile(b"\x00\x01")),
            ("SC4001E0-PSG.edf", ContentFile(b"0       ")),
            ("night.bdf", ContentFile(b"\xffBIOSEMI")),
            ("signals.mat", ContentFile(b"MATLAB")),
            ("labels.tsv", ContentFile(b"id\tlabel\n")),
            ("tool.exe", ContentFile(b"MZ")),
        ]
        with (
            override_settings(ALLOWED_EXTENSIONS=_as_set(_settings_default())),
            patch.object(services, "default_storage") as storage,
            patch.object(services, "invalidate_s3_caches"),
            patch.object(services, "logger"),
        ):
            result = services.upload_data_entries(project, entries=entries)

        self.assertEqual(
            result["saved"], [path for path, _ in entries if path != "tool.exe"]
        )
        self.assertEqual(
            result["skipped"],
            [{"path": "tool.exe", "reason": "Disallowed file extension."}],
        )
        saved_keys = [call.args[0] for call in storage.save.call_args_list]
        self.assertIn(f"{project.identifier}/data/fl-client-1/a0001.wav", saved_keys)
        self.assertEqual(len(saved_keys), len(entries) - 1)
