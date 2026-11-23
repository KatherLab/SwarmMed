"""The startup package's manifest and network fingerprint (network.fingerprint).

Most tests start from a network that SwarmMed's create_network provisioned with
NVIDIA FLARE (``nvflare provision``, no Docker) in a temporary folder, and import its
package at "another Hub": a second temporary folder, with the network's record
deleted for the duration of the test. Addresses are documentation examples.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import time
import tracemalloc
import unittest
import warnings
import zipfile
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from swarmed_cli.main import main as cli_main

from project.models import Project, UserCurrentProject

from . import fingerprint as fingerprint_module
from . import provision
from . import services as network_services
from .failover import network_prod_dir
from .fingerprint import (
    MANIFEST_FORMAT,
    MANIFEST_NAME,
    PackageVerificationError,
    build_manifest,
    canonical_bytes,
    certificate_fingerprint,
    check_package,
    group_fingerprint,
    network_fingerprint,
    normalize_fingerprint,
    unsafe_entry_problem,
)
from .models import SwarmNetwork, SwarmParticipant, UserCurrentNetwork
from .test_failover import NetworkFixture, _add_flare_files, _certificates, _make_kits

SITES = [
    {"name": "site-a", "ip": "100.64.0.11"},
    {"name": "site-b", "ip": "100.64.0.12"},
]
SERVER_IP = "100.64.0.10"


def _entries(package: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def _zip(entries: dict[str, bytes], *, symlinks=(), methods=None) -> bytes:
    """A ZIP of ``entries``, deflated unless ``methods`` gives a name another method."""
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate names, on purpose
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in (
                entries.items() if isinstance(entries, dict) else entries
            ):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                # writestr() takes the compression from the ZipInfo, not the archive.
                info.compress_type = (methods or {}).get(name, zipfile.ZIP_DEFLATED)
                mode = 0o120777 if name in symlinks else 0o100644
                info.external_attr = mode << 16
                archive.writestr(info, content)
    return buffer.getvalue()


def _change_kit(package: bytes, kit: str, change) -> bytes:
    """``package`` with ``change(files)`` applied to the files of one site's kit."""
    entries = _entries(package)
    files = _entries(entries[kit])
    change(files)
    entries[kit] = _zip(files)
    return _zip(entries)


def _replace_manifest(package: bytes, change) -> bytes:
    """``package`` with a changed manifest, written canonically into every copy."""
    entries = _entries(package)
    manifest = json.loads(entries[MANIFEST_NAME])
    change(manifest)
    new_manifest = canonical_bytes(manifest)
    entries[MANIFEST_NAME] = new_manifest
    for name in entries:
        if name.endswith(".zip"):
            files = _entries(entries[name])
            files[MANIFEST_NAME] = new_manifest
            entries[name] = _zip(files)
    return _zip(entries)


def _without_manifests(package: bytes) -> bytes:
    """``package`` as SwarmMed 1.0.4 exported it: without any manifest.json."""
    entries = _entries(package)
    del entries[MANIFEST_NAME]
    for name in entries:
        if name.endswith(".zip"):
            files = _entries(entries[name])
            del files[MANIFEST_NAME]
            entries[name] = _zip(files)
    return _zip(entries)


def _declared_sizes(package: bytes) -> list[int]:
    """The unpacked size of every entry, and of every entry of every site's kit."""
    sizes = []
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        for info in archive.infolist():
            sizes.append(info.file_size)
            if info.filename.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(archive.read(info))) as kit:
                    sizes.extend(inner.file_size for inner in kit.infolist())
    return sizes


def _alert_classes(page: str, text: str) -> str:
    """The classes of the message on ``page`` that contains ``text``."""
    for classes, message in re.findall(
        r'<div class="([^"]*)" role="alert">(.*?)</div>', page, re.S
    ):
        if text in message:
            return classes
    raise AssertionError(f"No message on the page contains {text!r}")


def _verify_startup(startup: Path) -> bool:
    """NVIDIA FLARE's check of a kit folder's signatures, as its verifier runs it.

    Like the verifier, it requires signature.json in the folder: FLARE 2.7.1 passes
    a folder without one (or with the default file name of the function).
    """
    from nvflare.lighter.constants import ProvFileName
    from nvflare.lighter.utils import verify_folder_signature

    if not (startup / ProvFileName.SIGNATURE_JSON).is_file():
        return False
    return verify_folder_signature(
        str(startup),
        str(startup / "rootCA.pem"),
        single_signer=True,
        signature_file=ProvFileName.SIGNATURE_JSON,
    )


def _secure_content_problems(startup: Path, site_type: str) -> list[str]:
    """What FLARE's start-up check in secure mode reports for a kit; [] passes."""
    from nvflare.fuel.sec.security_content_service import SecurityContentService
    from nvflare.private.fed.utils.fed_utils import _check_secure_content

    SecurityContentService.security_content_manager = None
    try:
        SecurityContentService.initialize(content_folder=str(startup))
        return _check_secure_content(site_type=site_type)
    finally:
        SecurityContentService.security_content_manager = None
        SecurityContentService.content_folder = None


class FingerprintHelperTests(SimpleTestCase):
    def test_canonical_json(self):
        self.assertEqual(
            canonical_bytes({"b": [1, "é"], "a": {"d": None, "c": True}}),
            '{"a":{"c":true,"d":null},"b":[1,"é"]}'.encode(),
        )
        with self.assertRaises(ValueError):
            canonical_bytes({"port": float("nan")})

    def test_fingerprint_is_the_sha256_of_the_canonical_manifest(self):
        manifest = {"format": MANIFEST_FORMAT, "files": {}}
        self.assertEqual(
            network_fingerprint(manifest),
            hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
        )

    def test_grouped_for_reading_aloud(self):
        fingerprint = "0123456789abcdef" * 4
        grouped = group_fingerprint(fingerprint)
        self.assertEqual(len(grouped.split(" ")), 16)
        self.assertTrue(grouped.startswith("0123 4567 89ab cdef 0123"))

    def test_typed_fingerprints_are_normalized(self):
        fingerprint = "0123456789abcdef" * 4
        for typed in (
            fingerprint,
            group_fingerprint(fingerprint).upper(),
            ":".join(fingerprint[index : index + 2] for index in range(0, 64, 2)),
            "-".join(group_fingerprint(fingerprint).split(" ")),
            f"  {group_fingerprint(fingerprint)}\n",
        ):
            with self.subTest(typed=typed):
                self.assertEqual(normalize_fingerprint(typed), fingerprint)
        for wrong in (fingerprint[:-1], fingerprint + "0", "g" * 64, ""):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                normalize_fingerprint(wrong)

    def test_certificate_fingerprint_has_openssls_format(self):
        root_ca, _ = _certificates()
        fingerprint = certificate_fingerprint(root_ca.encode())
        self.assertRegex(fingerprint, r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        openssl = shutil.which("openssl")
        if openssl is None:
            self.skipTest("openssl is not installed")
        with tempfile.TemporaryDirectory() as folder:
            pem = Path(folder) / "rootCA.pem"
            pem.write_text(root_ca)
            output = subprocess.run(  # nosec B603
                [openssl, "x509", "-noout", "-fingerprint", "-sha256", "-in", str(pem)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        self.assertEqual(output.strip().split("=", 1)[1], fingerprint)
        with self.assertRaises(ValueError):
            certificate_fingerprint(b"not a certificate")

    def test_unsafe_entries(self):
        for name, mode, problem in (
            ("/etc/passwd", 0o100644, "absolute"),
            ("C:/evil.txt", 0o100644, "absolute"),
            ("../evil.txt", 0o100644, "'..'"),
            ("startup/../../evil.txt", 0o100644, "'..'"),
            ("startup\\..\\evil.txt", 0o100644, "'..'"),
            ("startup/link", 0o120777, "symbolic link"),
            # zipfile would extract these as node-A.zip and startup/x.
            ("./node-A.zip", 0o100644, "empty or '.' component"),
            (".\\node-A.zip", 0o100644, "empty or '.' component"),
            ("startup//x", 0o100644, "empty or '.' component"),
            ("startup/./x", 0o100644, "empty or '.' component"),
            ("startup/.", 0o100644, "empty or '.' component"),
        ):
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            with self.subTest(name=name):
                self.assertIn(problem, unsafe_entry_problem(info))
        for name in (
            "startup/rootCA.pem",
            ".network_id",
            "node-A.zip",
            "a..b/c",
            "startup/",
            "startup/.gossip_token",
        ):
            with self.subTest(name=name):
                self.assertEqual(unsafe_entry_problem(zipfile.ZipInfo(name)), "")


class ManifestTests(SimpleTestCase):
    """build_manifest() on kits written by test_failover's helpers."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.prod_dir = Path(temp_dir.name) / "prod_00"
        _make_kits(self.prod_dir)
        _add_flare_files(self.prod_dir)
        participants = [
            {
                "participant_id": "node-A",
                "role": "CLIENT",
                "ip": "-",
                "org": "org_node_A",
            },
            {
                "participant_id": "server",
                "role": "SERVER",
                "ip": "100.64.0.1",
                "org": "x",
            },
        ]
        self.top = {
            ".network_id": b"network-1",
            ".participants.json": json.dumps(participants).encode(),
        }

    def _kits(self):
        kits = {}
        for site in ("node-A", "node-B"):
            files = {
                str(path.relative_to(self.prod_dir / site)): path.read_bytes()
                for path in (self.prod_dir / site).rglob("*")
                if path.is_file()
            }
            for path in (self.prod_dir / "server" / "startup").iterdir():
                files[f"server_startup/{path.name}"] = path.read_bytes()
            kits[site] = files
        return kits

    def _manifest(self, kits=None, top=None):
        return build_manifest(
            network_id="network-1",
            project="demo_study",
            top_files=top or self.top,
            kits=kits or self._kits(),
        )

    def test_the_same_kits_give_the_same_manifest(self):
        kits = self._kits()
        reordered = {
            site: dict(reversed(list(files.items())))
            for site, files in reversed(list(kits.items()))
        }
        first = self._manifest(kits)
        self.assertEqual(
            canonical_bytes(first), canonical_bytes(self._manifest(reordered))
        )
        # Nothing about the time, the exporting host or the SwarmMed version.
        self.assertEqual(
            set(first), {"format", "network_id", "project", "trust", "files"}
        )
        self.assertEqual(first["trust"]["server_address"], "100.64.0.1")
        self.assertEqual(
            (first["trust"]["fed_learn_port"], first["trust"]["admin_port"]),
            (8002, 8003),
        )
        self.assertEqual(
            [participant["name"] for participant in first["trust"]["participants"]],
            ["node-A", "server"],
        )

    def test_another_root_ca_gives_another_fingerprint(self):
        kits = self._kits()
        other_root_ca, _ = _certificates()
        substituted = {
            site: {
                path: other_root_ca.encode() if path.endswith("rootCA.pem") else content
                for path, content in files.items()
            }
            for site, files in kits.items()
        }
        first, second = self._manifest(kits), self._manifest(substituted)

        self.assertNotEqual(network_fingerprint(first), network_fingerprint(second))
        self.assertEqual(
            second["trust"]["root_ca_sha256"],
            certificate_fingerprint(other_root_ca.encode()),
        )

    def test_kits_naming_different_servers_are_refused(self):
        kits = self._kits()
        kits["node-B"]["startup/server_host.txt"] = b"192.0.2.66"
        with self.assertRaises(PackageVerificationError) as caught:
            self._manifest(kits)
        self.assertEqual(caught.exception.path, "node-B.zip/startup/server_host.txt")

    def test_a_server_participant_at_another_address_is_refused(self):
        participants = json.loads(self.top[".participants.json"])
        participants[1]["ip"] = "192.0.2.66"
        top = {**self.top, ".participants.json": json.dumps(participants).encode()}
        with self.assertRaises(PackageVerificationError) as caught:
            self._manifest(top=top)
        self.assertIn("192.0.2.66", str(caught.exception))

    def test_kits_with_different_root_cas_are_refused(self):
        kits = self._kits()
        other_root_ca, _ = _certificates()
        kits["node-B"]["admin_startup/rootCA.pem"] = other_root_ca.encode()
        with self.assertRaises(PackageVerificationError) as caught:
            self._manifest(kits)
        self.assertEqual(caught.exception.path, "node-B.zip/admin_startup/rootCA.pem")


@unittest.skipUnless(
    os.name == "posix", "runs nvflare provision through a shell script"
)
class ProvisionedNetworkTestCase(TestCase):
    """A network provisioned with NVIDIA FLARE at this, the coordinating, Hub."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        folder = Path(cls._folder.name)
        bin_dir = folder / "bin"
        bin_dir.mkdir()
        # SwarmMed runs the `nvflare` command; run it with this interpreter.
        command = bin_dir / "nvflare"
        command.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m nvflare.cli "$@"\n')
        command.chmod(0o755)
        cls._settings = override_settings(BASE_DIR=folder / "coordinator")
        cls._settings.enable()
        cls._patches = [
            patch.dict(
                os.environ,
                {
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "SWARMMEDHUB_HOSTNAME": "coordinator",
                },
            ),
            # Provisioning would read the project's requirements from the object store.
            patch(
                "common.utils.collect_project_runtime_requirements",
                return_value=["nvflare==2.7.1"],
            ),
        ]
        for started in cls._patches:
            started.start()
        os.environ.pop("SWARMMEDHUB_SERVER_HOST", None)
        try:
            super().setUpClass()
        except Exception:
            cls._undo()
            raise

    @classmethod
    def _undo(cls):
        for started in reversed(cls._patches):
            started.stop()
        cls._settings.disable()
        cls._folder.cleanup()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._undo()

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="coordinator", password="test-password"
        )  # nosec B106
        cls.user.profile.accepted_terms = True
        cls.user.profile.accepted_policy = True
        cls.user.profile.save(update_fields=["accepted_terms", "accepted_policy"])
        cls.project = Project.objects.create(title="Demo Study", author=cls.user)
        UserCurrentProject.objects.create(user=cls.user, project=cls.project)
        network = network_services.create_network(
            project=cls.project,
            actor=cls.user,
            name="Demo",
            participants=SITES,
            server_ip=SERVER_IP,
            include_local_client=False,
        )
        cls.prod_dir = network_prod_dir(network)
        if not (cls.prod_dir / "site-a" / "startup").is_dir():
            raise RuntimeError(f"nvflare provision failed (network {network.status})")
        # Provisioning set the network's gossip token on another instance.
        network.refresh_from_db()
        cls.package = network_services.export_startup_package(network).getvalue()
        cls.network = network
        cls.fingerprint = network.package_fingerprint
        cls.root_ca = certificate_fingerprint(
            (cls.prod_dir / "server" / "startup" / "rootCA.pem").read_bytes()
        )

    @contextmanager
    def other_hub(self):
        """Another Hub: a folder of its own, and no record of the network."""
        SwarmNetwork.objects.filter(identifier=self.network.identifier).delete()
        with (
            tempfile.TemporaryDirectory() as folder,
            override_settings(BASE_DIR=Path(folder)),
        ):
            yield Path(folder)

    def import_package(self, package: bytes, **kwargs) -> SwarmNetwork:
        return network_services.import_network(
            project=self.project,
            actor=self.user,
            name="Imported",
            package_source=io.BytesIO(package),
            **kwargs,
        )

    def assert_refused(self, package: bytes, *expected_text, **kwargs) -> str:
        """Import ``package`` at another Hub: refused, with nothing written."""
        with self.other_hub() as hub:
            with self.assertRaises(ValueError) as caught:
                self.import_package(package, **kwargs)
            self.assertFalse(
                SwarmNetwork.objects.filter(identifier=self.network.identifier).exists()
            )
            self.assertFalse((hub / "workspaces").exists())
        message = str(caught.exception)
        for text in expected_text:
            self.assertIn(text, message)
        return message

    def provision_again(self) -> bytes:
        """The same network provisioned again (a new root CA and keys), exported."""
        rows = list(
            self.network.participants.values("role", "participant_id", "org", "ip")
        )
        SwarmNetwork.objects.filter(identifier=self.network.identifier).delete()
        with (
            tempfile.TemporaryDirectory() as folder,
            override_settings(BASE_DIR=Path(folder)),
        ):
            network = SwarmNetwork.objects.create(
                identifier=self.network.identifier,
                name="Demo",
                project=self.project,
                author=self.user,
                gossip_token=self.network.gossip_token,
            )
            for row in rows:
                SwarmParticipant.objects.create(network=network, user=self.user, **row)
            provision.generate_flare_startup_kit(
                network_id=network.identifier, clients=SITES, server_ip=SERVER_IP
            )
            package = network_services.export_startup_package(network).getvalue()
        SwarmNetwork.objects.filter(identifier=self.network.identifier).delete()
        return package


class ProvisionedKitTests(ProvisionedNetworkTestCase):
    def test_fresh_kits_pass_flares_verifier_and_start_check(self):
        kits = sorted(
            path
            for path in self.prod_dir.iterdir()
            if (path / "startup" / "signature.json").is_file()
        )
        self.assertEqual(
            [path.name for path in kits],
            [
                "admin-site-a@nvidia.com",
                "admin-site-b@nvidia.com",
                "server",
                "site-a",
                "site-b",
            ],
        )
        for kit in kits:
            with self.subTest(kit=kit.name):
                # SwarmMed's own files are in the folder, and covered.
                self.assertEqual(
                    (kit / "startup" / "server_host.txt").read_text(), SERVER_IP
                )
                self.assertTrue((kit / "startup" / ".all_participants.json").is_file())
                self.assertTrue(_verify_startup(kit / "startup"))
        self.assertTrue(
            (self.prod_dir / "site-a" / "startup" / "server_aliases.txt").is_file()
        )

        site = self.prod_dir / "site-a" / "startup"
        verifier = subprocess.run(  # nosec B603
            [
                sys.executable,
                "-m",
                "nvflare.tool.verify_startup_kits",
                "-f",
                str(site),
                "-c",
                str(site / "rootCA.pem"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(verifier.returncode, 0, verifier.stderr)
        self.assertEqual(_secure_content_problems(site, "client"), [])
        self.assertEqual(
            _secure_content_problems(self.prod_dir / "server" / "startup", "server"), []
        )

    def test_a_changed_server_address_is_reported_by_flares_verifier(self):
        with tempfile.TemporaryDirectory() as folder:
            startup = Path(folder) / "startup"
            shutil.copytree(self.prod_dir / "site-a" / "startup", startup)
            (startup / "server_host.txt").write_text("192.0.2.66")

            self.assertFalse(_verify_startup(startup))
            # FLARE's start-up check does not cover the file.
            self.assertEqual(_secure_content_problems(startup, "client"), [])

    def test_signing_again_covers_files_changed_since(self):
        with tempfile.TemporaryDirectory() as folder:
            prod_dir = Path(folder) / "prod_00"
            shutil.copytree(self.prod_dir / "site-b", prod_dir / "site-b")
            (prod_dir / "notes" / "startup").mkdir(parents=True)  # not a signed kit
            startup = prod_dir / "site-b" / "startup"
            (startup / "server_host.txt").write_text("192.0.2.10")
            self.assertFalse(_verify_startup(startup))

            signed = provision.sign_startup_folders(
                prod_dir, self.prod_dir.parent / "state" / "cert.json"
            )

            self.assertEqual(signed, [str(startup)])
            self.assertTrue(_verify_startup(startup))
            self.assertFalse(
                (prod_dir / "notes" / "startup" / "signature.json").exists()
            )


class ExportTests(ProvisionedNetworkTestCase):
    def test_package_layout_and_manifest(self):
        entries = _entries(self.package)
        network_dir = self.prod_dir.parents[2]

        self.assertEqual(
            sorted(entries),
            [
                ".gossip_token",
                ".network_id",
                ".participants.json",
                MANIFEST_NAME,
                "project.yml",
                "runtime_requirements.txt",
                "site-a.zip",
                "site-b.zip",
            ],
        )
        self.assertEqual(
            entries["project.yml"], (network_dir / "project.yml").read_bytes()
        )
        self.assertEqual(
            entries["runtime_requirements.txt"],
            (network_dir / "runtime_requirements.txt").read_bytes(),
        )
        manifest_bytes = entries[MANIFEST_NAME]
        manifest = json.loads(manifest_bytes)
        self.assertEqual(manifest_bytes, canonical_bytes(manifest))
        self.assertEqual(self.fingerprint, hashlib.sha256(manifest_bytes).hexdigest())
        self.assertEqual(self.fingerprint, network_fingerprint(manifest))
        self.assertEqual(
            self.network.package_sha256, hashlib.sha256(self.package).hexdigest()
        )
        self.assertEqual(manifest["format"], MANIFEST_FORMAT)
        self.assertEqual(manifest["network_id"], str(self.network.identifier))
        self.assertEqual(manifest["project"], "demo_study")

        trust = manifest["trust"]
        self.assertEqual(trust["root_ca_sha256"], self.root_ca)
        self.assertEqual(
            trust["server_certificate_sha256"],
            certificate_fingerprint(
                (self.prod_dir / "server" / "startup" / "server.crt").read_bytes()
            ),
        )
        self.assertEqual(trust["server_address"], SERVER_IP)
        self.assertEqual(
            (trust["fed_learn_port"], trust["admin_port"]),
            (provision.FED_LEARN_PORT, provision.ADMIN_PORT),
        )
        self.assertEqual(
            [(p["name"], p["role"], p["address"]) for p in trust["participants"]],
            [
                ("server", "SERVER", SERVER_IP),
                ("site-a", "CLIENT", "100.64.0.11"),
                ("site-b", "CLIENT", "100.64.0.12"),
            ],
        )
        self.assertEqual(
            trust["participants"][1]["certificate_sha256"],
            certificate_fingerprint(
                (self.prod_dir / "site-a" / "startup" / "client.crt").read_bytes()
            ),
        )

        files = manifest["files"]
        for path in (
            "project.yml",
            "site-a.zip/startup/rootCA.pem",
            "site-b.zip/server_startup/server.key",
        ):
            self.assertIn(path, files)
        self.assertFalse([path for path in files if path.endswith(MANIFEST_NAME)])
        for kit in ("site-a.zip", "site-b.zip"):
            kit_entries = _entries(entries[kit])
            with self.subTest(kit=kit):
                self.assertEqual(kit_entries[MANIFEST_NAME], manifest_bytes)
                self.assertIn("runtime_requirements.txt", kit_entries)
                for name, content in kit_entries.items():
                    if name != MANIFEST_NAME:
                        self.assertEqual(
                            files[f"{kit}/{name}"], hashlib.sha256(content).hexdigest()
                        )

        with zipfile.ZipFile(io.BytesIO(self.package)) as archive:
            infos = archive.infolist()
        self.assertEqual([info.filename for info in infos], sorted(entries))
        self.assertEqual({info.date_time for info in infos}, {(1980, 1, 1, 0, 0, 0)})

    def test_exporting_again_gives_the_same_package(self):
        # Other file times and modes, and the participants created in another order.
        for path in self.prod_dir.rglob("*"):
            os.utime(path, (1_000_000_000, 1_000_000_000))
        (self.prod_dir / "site-a" / "readme.txt").chmod(0o666)
        rows = list(
            self.network.participants.values(
                "user_id", "role", "participant_id", "org", "ip"
            )
        )
        self.network.participants.all().delete()
        for row in reversed(rows):
            SwarmParticipant.objects.create(network=self.network, **row)
        later = time.time() + 3 * 86400

        with patch.object(
            zipfile,
            "time",
            SimpleNamespace(time=lambda: later, localtime=time.localtime),
        ):
            second = network_services.export_startup_package(self.network).getvalue()

        self.assertEqual(second, self.package)
        self.network.refresh_from_db()
        self.assertEqual(self.network.package_fingerprint, self.fingerprint)

    def test_serialized_network_has_the_package_values(self):
        # As stored at the export, not as set on the exported instance.
        network = SwarmNetwork.objects.get(pk=self.network.pk)
        serialized = network_services.serialize_network(network)

        self.assertEqual(serialized["package_fingerprint"], self.fingerprint)
        self.assertEqual(
            serialized["package_sha256"], hashlib.sha256(self.package).hexdigest()
        )
        self.assertEqual(serialized["root_ca_sha256"], self.root_ca)


class ImportTests(ProvisionedNetworkTestCase):
    def test_importing_hub_gets_the_same_fingerprint(self):
        with self.other_hub():
            imported = self.import_package(self.package)
            prod_dir = network_prod_dir(imported)

            self.assertEqual(imported.package_fingerprint, self.fingerprint)
            self.assertEqual(
                imported.package_sha256, hashlib.sha256(self.package).hexdigest()
            )
            self.assertEqual(network_services.import_warnings(imported), [])
            serialized = network_services.serialize_network(imported)
            self.assertEqual(serialized["package_fingerprint"], self.fingerprint)
            self.assertEqual(serialized["root_ca_sha256"], self.root_ca)
            # The manifest stays in the network's folder, out of the sites' workspaces.
            self.assertTrue((prod_dir.parents[2] / MANIFEST_NAME).is_file())
            self.assertEqual(list(prod_dir.glob(f"*/{MANIFEST_NAME}")), [])
            self.assertTrue((prod_dir.parents[2] / "project.yml").is_file())
            # The kits as imported still pass FLARE's verifier.
            self.assertTrue(_verify_startup(prod_dir / "site-a" / "startup"))
            self.assertTrue(_verify_startup(prod_dir / "server" / "startup"))

    def test_an_importing_hub_recognises_its_site_by_its_address(self):
        # The package carries project.yml, which gives each client's address.
        from .tasks import _resolve_local_client_names

        with self.other_hub():
            imported = self.import_package(self.package)
            prod_dir = network_prod_dir(imported)
            with patch(
                "network.tasks.get_local_addresses", return_value={"100.64.0.12"}
            ):
                names = _resolve_local_client_names(
                    str(prod_dir.parents[2] / "project.yml"), str(prod_dir)
                )

        self.assertIn("site-b", names)
        self.assertNotIn("site-a", names)

    def test_a_server_address_from_the_environment_is_reported_by_flares_verifier(self):
        with (
            self.other_hub(),
            patch.dict(os.environ, {"SWARMMEDHUB_SERVER_HOST": "192.0.2.10"}),
        ):
            imported = self.import_package(self.package)
            startup = network_prod_dir(imported) / "site-a" / "startup"

            self.assertEqual(imported.package_fingerprint, self.fingerprint)
            self.assertEqual((startup / "server_host.txt").read_text(), "192.0.2.10")
            self.assertFalse(_verify_startup(startup))

    def test_a_single_sites_kit_shows_the_same_fingerprint(self):
        kit = _entries(self.package)["site-b.zip"]
        with self.other_hub():
            imported = self.import_package(kit)
            self.assertEqual(imported.package_fingerprint, self.fingerprint)
            self.assertEqual(imported.package_sha256, hashlib.sha256(kit).hexdigest())
            prod_dir = network_prod_dir(imported)
            self.assertTrue((prod_dir / "startup" / "client.crt").is_file())
            self.assertFalse((prod_dir / MANIFEST_NAME).exists())
            self.assertTrue((prod_dir.parents[2] / MANIFEST_NAME).is_file())

    def test_a_changed_single_sites_kit_is_refused(self):
        files = _entries(_entries(self.package)["site-b.zip"])
        files["startup/server_host.txt"] = b"192.0.2.66"
        self.assert_refused(
            _zip(files),
            "site-b.zip/startup/server_host.txt does not match manifest.json",
        )

    def test_a_single_kit_that_matches_no_site_is_refused(self):
        files = _entries(_entries(self.package)["site-b.zip"])
        _, certificates = _certificates("site-b")
        files["startup/client.crt"] = certificates["site-b"].encode()
        self.assert_refused(_zip(files), "startup/client.crt matches no site's kit")

    def test_expected_fingerprint(self):
        for typed in (
            self.fingerprint,
            group_fingerprint(self.fingerprint).upper(),
            ":".join(
                self.fingerprint[index : index + 2] for index in range(0, 64, 2)
            ).upper(),
        ):
            with self.subTest(typed=typed), self.other_hub():
                imported = self.import_package(self.package, expect_fingerprint=typed)
                self.assertEqual(imported.package_fingerprint, self.fingerprint)

    def test_another_expected_fingerprint_is_refused(self):
        other = "0" * 64
        message = self.assert_refused(
            self.package,
            group_fingerprint(self.fingerprint),
            group_fingerprint(other),
            "Nothing was imported",
            expect_fingerprint=other,
        )
        self.assertNotIn("root CA", message)

    def test_the_root_cas_fingerprint_as_expected_value_is_refused_with_a_hint(self):
        self.assert_refused(
            self.package,
            "root CA certificate's fingerprint",
            expect_fingerprint=self.root_ca,
        )

    def test_a_malformed_expected_fingerprint_is_refused(self):
        self.assert_refused(
            self.package, "64 hexadecimal", expect_fingerprint=self.fingerprint[:-1]
        )

    def test_an_empty_expected_fingerprint_is_refused(self):
        # As a script passes an unset variable: malformed, not "no expectation".
        for package in (self.package, _without_manifests(self.package)):
            with self.subTest(manifest=package is self.package):
                self.assert_refused(package, "64 hexadecimal", expect_fingerprint="")

    def test_a_hub_that_imported_a_single_sites_kit_cannot_export_the_package(self):
        kit = _entries(self.package)["site-b.zip"]
        with self.other_hub():
            imported = self.import_package(kit)

            with self.assertRaises(LookupError) as caught:
                network_services.export_startup_package(imported)

            # The values of the kit it imported, not of a package without kits.
            imported.refresh_from_db()
            self.assertEqual(imported.package_fingerprint, self.fingerprint)
            self.assertEqual(imported.package_sha256, hashlib.sha256(kit).hexdigest())
        self.assertIn("imported a single site's kit", str(caught.exception))
        self.assertIn("export it at the coordinating site", str(caught.exception))

    def test_a_package_without_manifest_imports_with_a_warning(self):
        legacy = _without_manifests(self.package)
        with self.other_hub():
            imported = self.import_package(legacy)

            self.assertEqual(imported.package_fingerprint, "")
            self.assertEqual(
                imported.package_sha256, hashlib.sha256(legacy).hexdigest()
            )
            warnings_shown = network_services.import_warnings(imported)
            self.assertEqual(len(warnings_shown), 1)
            self.assertIn("no manifest.json", warnings_shown[0])
            self.assertIn(imported.package_sha256, warnings_shown[0])
            self.assertTrue(
                (
                    network_prod_dir(imported) / "site-a" / "startup" / "client.crt"
                ).is_file()
            )

    def test_a_package_without_manifest_is_refused_with_an_expected_fingerprint(self):
        self.assert_refused(
            _without_manifests(self.package),
            "has no manifest.json",
            expect_fingerprint=self.fingerprint,
        )

    def test_a_self_consistent_substitute_is_caught_by_the_expected_fingerprint(self):
        # A package of the same network from another provisioning (another root CA)
        # agrees with its own manifest; only its fingerprint differs.
        substitute = self.provision_again()
        substitute_fingerprint = json.loads(_entries(substitute)[MANIFEST_NAME])
        self.assertNotEqual(
            network_fingerprint(substitute_fingerprint), self.fingerprint
        )
        self.assertNotEqual(
            substitute_fingerprint["trust"]["root_ca_sha256"], self.root_ca
        )
        with self.other_hub():
            imported = self.import_package(substitute)
            self.assertNotEqual(imported.package_fingerprint, self.fingerprint)
        self.assert_refused(
            substitute, "not the expected", expect_fingerprint=self.fingerprint
        )
        # Its kit, in the package exported here, is refused outright.
        entries = _entries(self.package)
        entries["site-a.zip"] = _entries(substitute)["site-a.zip"]
        self.assert_refused(_zip(entries), "site-a.zip/manifest.json differs")


class TamperedPackageTests(ProvisionedNetworkTestCase):
    """Each tampered package is refused at its first offending path; nothing written."""

    def test_a_changed_file(self):
        def change(files):
            files["startup/server_host.txt"] = b"192.0.2.66"

        self.assert_refused(
            _change_kit(self.package, "site-a.zip", change),
            "site-a.zip/startup/server_host.txt does not match manifest.json",
        )

    def test_a_changed_start_script(self):
        def change(files):
            files["startup/sub_start.sh"] = files["startup/sub_start.sh"].replace(
                b"secure_train=true", b"secure_train=false"
            )

        self.assert_refused(
            _change_kit(self.package, "site-b.zip", change),
            "site-b.zip/startup/sub_start.sh does not match",
        )

    def test_a_changed_top_level_file(self):
        entries = _entries(self.package)
        entries[".participants.json"] = entries[".participants.json"].replace(
            b"100.64.0.12", b"192.0.2.66"
        )
        self.assert_refused(_zip(entries), ".participants.json does not match")

    def test_an_added_file(self):
        def change(files):
            files["startup/extra.sh"] = b"echo hello"

        self.assert_refused(
            _change_kit(self.package, "site-b.zip", change),
            "site-b.zip/startup/extra.sh is not listed in manifest.json",
        )

    def test_an_added_top_level_entry(self):
        entries = _entries(self.package)
        entries["notes.txt"] = b"hello"
        self.assert_refused(_zip(entries), "notes.txt is not listed in manifest.json")

    def test_an_added_site_kit(self):
        entries = _entries(self.package)
        entries["site-c.zip"] = entries["site-b.zip"]
        self.assert_refused(_zip(entries), "site-c.zip is not listed in manifest.json")

    def test_a_removed_file(self):
        def change(files):
            del files["local/resources.json.default"]

        self.assert_refused(
            _change_kit(self.package, "site-a.zip", change),
            "site-a.zip/local/resources.json.default is listed in manifest.json "
            "but missing",
        )

    def test_a_removed_site_kit(self):
        entries = _entries(self.package)
        del entries["site-b.zip"]
        self.assert_refused(
            _zip(entries), "site-b.zip is listed in manifest.json but missing"
        )

    def test_a_kit_whose_manifest_differs(self):
        manifest = json.loads(_entries(self.package)[MANIFEST_NAME])
        manifest["trust"]["server_address"] = "192.0.2.66"

        def change(files):
            files[MANIFEST_NAME] = canonical_bytes(manifest)

        self.assert_refused(
            _change_kit(self.package, "site-b.zip", change),
            "site-b.zip/manifest.json differs from the package's manifest.json",
        )

    def test_a_kit_without_manifest(self):
        def change(files):
            del files[MANIFEST_NAME]

        self.assert_refused(
            _change_kit(self.package, "site-a.zip", change),
            "site-a.zip/manifest.json is missing",
        )

    def test_a_package_whose_manifest_was_removed_but_not_its_kits(self):
        entries = _entries(self.package)
        del entries[MANIFEST_NAME]
        self.assert_refused(_zip(entries), "manifest.json is missing, although")

    def test_a_package_that_an_older_version_exported_again(self):
        # As SwarmMed 1.0.4 exports a network imported from this package: the kits
        # carry the manifest it extracted into them, the package itself has none.
        entries = _entries(self.package)
        for name in (MANIFEST_NAME, "project.yml", "runtime_requirements.txt"):
            del entries[name]
        self.assert_refused(
            _zip(entries),
            "manifest.json is missing, although site-a.zip/manifest.json is there",
            "SwarmMed 1.0.4 or earlier exported it again from a network imported "
            "from a later version",
            "use the package that the coordinating site exported. Nothing was "
            "imported.",
        )

    def test_a_trust_section_that_disagrees_with_the_files(self):
        other_root_ca, _ = _certificates()

        def change(manifest):
            manifest["trust"]["root_ca_sha256"] = certificate_fingerprint(
                other_root_ca.encode()
            )

        self.assert_refused(
            _replace_manifest(self.package, change),
            "manifest.json gives trust.root_ca_sha256",
            "site-a.zip/admin_startup/rootCA.pem",
        )

    def test_a_trust_section_with_other_ports(self):
        def change(manifest):
            manifest["trust"]["fed_learn_port"] = 9002

        self.assert_refused(
            _replace_manifest(self.package, change),
            "trust.fed_learn_port",
            "server_startup/fed_server.json",
        )

    def test_a_trust_section_with_another_participant_address(self):
        def change(manifest):
            manifest["trust"]["participants"][2]["address"] = "192.0.2.66"

        self.assert_refused(
            _replace_manifest(self.package, change), "address '192.0.2.66'", "'site-b'"
        )

    def test_a_redirected_server_with_a_rebuilt_manifest(self):
        # Every server_host.txt changed and the manifest rebuilt to match: the
        # package names another server than its SERVER participant.
        entries = _entries(self.package)
        files = json.loads(entries[MANIFEST_NAME])["files"]
        for kit in ("site-a.zip", "site-b.zip"):
            kit_files = _entries(entries[kit])
            for name in kit_files:
                if name.endswith("server_host.txt"):
                    kit_files[name] = b"192.0.2.66"
                    files[f"{kit}/{name}"] = hashlib.sha256(b"192.0.2.66").hexdigest()
            entries[kit] = _zip(kit_files)
        package = _zip(entries)

        def change(manifest):
            manifest["files"] = files
            manifest["trust"]["server_address"] = "192.0.2.66"

        self.assert_refused(
            _replace_manifest(package, change),
            "names the FL server 192.0.2.66",
            "gives the SERVER participant the address 100.64.0.10",
        )

    def test_a_network_id_that_disagrees(self):
        def change(manifest):
            manifest["network_id"] = "11111111-1111-1111-1111-111111111111"

        self.assert_refused(
            _replace_manifest(self.package, change), "manifest.json gives the network"
        )

    def test_a_manifest_that_is_not_canonical(self):
        entries = _entries(self.package)
        manifest = json.loads(entries[MANIFEST_NAME])
        entries[MANIFEST_NAME] = json.dumps(manifest, indent=2, sort_keys=True).encode()
        self.assert_refused(_zip(entries), "manifest.json is not in the canonical form")

    def test_a_manifest_of_another_format(self):
        def change(manifest):
            manifest["format"] = "swarmmed-package-manifest/2"

        self.assert_refused(
            _replace_manifest(self.package, change), "'swarmmed-package-manifest/2'"
        )

    def test_a_duplicate_entry(self):
        package = _entries(self.package)
        for name, content in (
            (".gossip_token", b"another token"),
            # Read as the last of the two; the check reads one manifest only.
            (MANIFEST_NAME, package[MANIFEST_NAME]),
        ):
            with self.subTest(name=name):
                entries = [*package.items(), (name, content)]
                self.assert_refused(_zip(entries), f"{name} appears more than once")

    def test_an_entry_in_a_folder_named_like_a_kit(self):
        entries = _entries(self.package)
        entries["site-a.zip/startup/extra.sh"] = b"echo hello"
        self.assert_refused(_zip(entries), "lies in a folder named like the site kit")

    def test_a_file_that_is_not_a_zip(self):
        self.assert_refused(b"not a zip file", "cannot be read as a ZIP file")


class UnsafeEntryTests(ProvisionedNetworkTestCase):
    """Unsafe entries are refused, with or without a manifest, with nothing written."""

    def test_unsafe_entries_are_refused(self):
        for package_kind, package in (
            ("with manifest", self.package),
            ("without manifest", _without_manifests(self.package)),
        ):
            for name, symlinks, text in (
                ("../evil.txt", (), "../evil.txt contains a '..' component"),
                ("/tmp/evil.txt", (), "/tmp/evil.txt is an absolute path"),
                ("startup-link", ("startup-link",), "startup-link is a symbolic link"),
            ):
                entries = _entries(package)
                entries[name] = b"evil"
                with self.subTest(package=package_kind, name=name):
                    self.assert_refused(_zip(entries, symlinks=symlinks), text)

            def change(files):
                files["../../evil.txt"] = b"evil"

            with self.subTest(package=package_kind, name="in a kit"):
                self.assert_refused(
                    _change_kit(package, "site-a.zip", change),
                    "site-a.zip/../../evil.txt contains a '..' component",
                )

            # A kit that an import would unpack, but a check could pass over: a
            # second copy of a kit, or one named so that zipfile extracts it as
            # site-c.zip.
            evil_kit = _zip(
                {"startup/client.crt": b"certificate", "../../evil.txt": b"evil"}
            )
            for name, text in (
                ("site-a.zip", "site-a.zip/../../evil.txt contains a '..' component"),
                ("./site-c.zip", "./site-c.zip contains an empty or '.' component"),
            ):
                entries = [*_entries(package).items(), (name, evil_kit)]
                with self.subTest(package=package_kind, name=f"another {name}"):
                    self.assert_refused(_zip(entries), text)


class SizeLimitTests(ProvisionedNetworkTestCase):
    """What an import reads is bounded by the sizes that the package declares."""

    def test_a_file_larger_than_an_import_accepts_is_refused(self):
        largest = max(_declared_sizes(self.package))
        # The other entries stay within the limit, the kit holding this one too.
        oversized = b"x" * (4 * largest)
        for package_kind, package in (
            ("with manifest", self.package),
            ("without manifest", _without_manifests(self.package)),
        ):
            entries = _entries(package)
            entries["notes.bin"] = oversized

            def change(files):
                files["startup/notes.bin"] = oversized

            in_kit = _change_kit(package, "site-a.zip", change)
            for tampered, path in (
                (_zip(entries), "notes.bin"),
                (in_kit, "site-a.zip/startup/notes.bin"),
            ):
                with (
                    self.subTest(package=package_kind, path=path),
                    patch.object(fingerprint_module, "MAX_ENTRY_BYTES", 2 * largest),
                ):
                    self.assert_refused(
                        tampered,
                        f"{path} is {4 * largest} bytes unpacked, more than the "
                        f"{2 * largest} an import accepts for one file",
                    )

    def test_a_package_that_unpacks_to_more_than_an_import_accepts_is_refused(self):
        for package_kind, package in (
            ("with manifest", self.package),
            ("without manifest", _without_manifests(self.package)),
        ):
            total = sum(_declared_sizes(package))
            entries = _entries(package)
            entries["notes.txt"] = b"x"
            with (
                self.subTest(package=package_kind),
                patch.object(fingerprint_module, "MAX_PACKAGE_BYTES", total),
            ):
                # The package as exported fits exactly.
                with zipfile.ZipFile(io.BytesIO(package)) as archive:
                    check_package(archive)
                self.assert_refused(
                    _zip(entries), f"takes the package over the {total} bytes unpacked"
                )

    def test_entries_compressed_with_other_methods_are_refused(self):
        # zipfile decompresses bzip2 and LZMA data whole, whatever size an entry
        # declares. These entries are honest: their content agrees with the
        # manifest.
        for package_kind, package in (
            ("with manifest", self.package),
            ("without manifest", _without_manifests(self.package)),
        ):
            for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
                entries = _entries(package)
                at_top = _zip(entries, methods={"runtime_requirements.txt": method})
                kit = _zip(
                    _entries(entries["site-a.zip"]),
                    methods={"startup/fed_client.json": method},
                )
                in_kit = _zip({**entries, "site-a.zip": kit})
                for tampered, path in (
                    (at_top, "runtime_requirements.txt"),
                    (in_kit, "site-a.zip/startup/fed_client.json"),
                ):
                    with self.subTest(package=package_kind, method=method, path=path):
                        self.assert_refused(
                            tampered,
                            f"{path} is compressed with method {method}; an import "
                            "accepts only stored and deflated entries",
                        )

    def test_a_package_file_larger_than_an_import_accepts_is_refused(self):
        limit = len(self.package) - 1
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "package.zip"
            path.write_bytes(self.package)
            for source_kind, source in (
                ("file object", lambda: io.BytesIO(self.package)),
                ("path", lambda: str(path)),
                (
                    "upload",
                    lambda: SimpleUploadedFile(
                        "package.zip", self.package, "application/zip"
                    ),
                ),
            ):
                with (
                    self.subTest(source=source_kind),
                    patch.object(network_services, "MAX_PACKAGE_BYTES", limit),
                    self.other_hub() as hub,
                ):
                    with self.assertRaises(ValueError) as caught:
                        network_services.import_network(
                            project=self.project,
                            actor=self.user,
                            name="Imported",
                            package_source=source(),
                        )
                    self.assertIn(
                        f"larger than the {limit} bytes that an import accepts",
                        str(caught.exception),
                    )
                    self.assertFalse(SwarmNetwork.objects.exists())
                    self.assertFalse((hub / "workspaces").exists())
        # A package of exactly the limit is imported.
        with (
            patch.object(network_services, "MAX_PACKAGE_BYTES", len(self.package)),
            self.other_hub(),
        ):
            imported = self.import_package(self.package)
            self.assertEqual(imported.package_fingerprint, self.fingerprint)

    def test_a_file_the_manifest_does_not_list_is_refused_before_it_is_read(self):
        opened = []
        original_open = zipfile.ZipFile.open

        def recording_open(archive, name, *args, **kwargs):
            opened.append(getattr(name, "filename", name))
            return original_open(archive, name, *args, **kwargs)

        entries = _entries(self.package)
        entries["notes.bin"] = b"x" * 4096

        def change(files):
            files["startup/notes.bin"] = b"x" * 4096

        in_kit = _change_kit(self.package, "site-a.zip", change)
        for tampered, path in (
            (_zip(entries), "notes.bin"),
            (in_kit, "site-a.zip/startup/notes.bin"),
        ):
            opened.clear()
            with (
                self.subTest(path=path),
                patch.object(zipfile.ZipFile, "open", recording_open),
            ):
                self.assert_refused(tampered, f"{path} is not listed in manifest.json")
                self.assertIn("site-a.zip", opened)  # the check did open entries
                self.assertNotIn(path.rsplit(".zip/", 1)[-1], opened)

    def test_files_are_hashed_without_holding_them(self):
        big = bytes(48 * 1024 * 1024)

        def change(files):
            files["local/big.bin"] = big

        def list_it(manifest):
            manifest["files"]["site-a.zip/local/big.bin"] = hashlib.sha256(
                big
            ).hexdigest()

        package = _replace_manifest(
            _change_kit(self.package, "site-a.zip", change), list_it
        )
        manifest_bytes = _entries(package)[MANIFEST_NAME]

        was_tracing = tracemalloc.is_tracing()
        if not was_tracing:
            tracemalloc.start()
        tracemalloc.reset_peak()
        baseline, _peak = tracemalloc.get_traced_memory()
        try:
            with zipfile.ZipFile(io.BytesIO(package)) as archive:
                check = check_package(archive)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            if not was_tracing:
                tracemalloc.stop()

        self.assertEqual(check.fingerprint, hashlib.sha256(manifest_bytes).hexdigest())
        # Far less than the file: it was read a chunk at a time and not kept.
        self.assertLess(peak - baseline, 16 * 1024 * 1024)


class CLITests(ProvisionedNetworkTestCase):
    def _cli(self, *args, json_output=True):
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = cli_main(
                [
                    "network",
                    *args,
                    "--user",
                    "coordinator",
                    *(["--json"] if json_output else []),
                ]
            )
        output = stdout.getvalue()
        return code, (json.loads(output) if json_output else output)

    def test_export_package_gives_the_fingerprint_and_the_files_sha256(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "package.zip"
            code, payload = self._cli(
                "export-package", str(self.network.identifier), "--out", str(out)
            )
            written = out.read_bytes()

        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["result"]["fingerprint"], self.fingerprint)
        self.assertEqual(
            payload["result"]["package_sha256"], hashlib.sha256(written).hexdigest()
        )
        self.assertEqual(written, self.package)

    def test_export_package_prints_the_grouped_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            code, output = self._cli(
                "export-package",
                str(self.network.identifier),
                "--out",
                str(Path(folder) / "package.zip"),
                json_output=False,
            )

        self.assertEqual(code, 0, output)
        self.assertIn(
            f"Network fingerprint: {group_fingerprint(self.fingerprint)}\n", output
        )
        self.assertIn("by phone or video call", output)

    @patch("network.runtime.get_local_participant_status", return_value={})
    def test_show_and_status_give_the_package_values(self, _status):
        for command in ("show", "status"):
            with self.subTest(command=command):
                code, payload = self._cli(command, str(self.network.identifier))
                self.assertEqual(code, 0, payload)
                self.assertEqual(
                    payload["result"]["package_fingerprint"], self.fingerprint
                )
                self.assertEqual(
                    payload["result"]["package_sha256"],
                    hashlib.sha256(self.package).hexdigest(),
                )
                self.assertEqual(payload["result"]["root_ca_sha256"], self.root_ca)
                code, output = self._cli(
                    command, str(self.network.identifier), json_output=False
                )
                self.assertIn(group_fingerprint(self.fingerprint), output)

    def _import(self, package: bytes, *args, json_output=True):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "package.zip"
            path.write_bytes(package)
            return self._cli(
                "import",
                "--project",
                str(self.project.identifier),
                "--name",
                "Imported",
                "--package",
                str(path),
                *args,
                json_output=json_output,
            )

    def test_import_with_the_expected_fingerprint(self):
        with self.other_hub():
            code, payload = self._import(
                self.package,
                "--expect-fingerprint",
                group_fingerprint(self.fingerprint),
            )
            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["result"]["package_fingerprint"], self.fingerprint)
            self.assertEqual(payload["result"]["root_ca_sha256"], self.root_ca)
            self.assertEqual(payload["warnings"], [])

    def test_import_prints_the_grouped_fingerprint(self):
        with self.other_hub():
            code, output = self._import(self.package, json_output=False)
        self.assertEqual(code, 0, output)
        self.assertIn(
            f"Network fingerprint: {group_fingerprint(self.fingerprint)}", output
        )

    def test_import_with_another_fingerprint_is_refused(self):
        with self.other_hub() as hub:
            code, payload = self._import(
                self.package, "--expect-fingerprint", "ab" * 32
            )
            self.assertEqual(code, 2)
            self.assertIn("not the expected", payload["errors"][0])
            self.assertFalse(SwarmNetwork.objects.exists())
            self.assertFalse((hub / "workspaces").exists())

    def test_import_with_an_empty_expected_fingerprint_is_refused(self):
        # As `--expect-fingerprint "$FP"` passes an unset variable.
        with self.other_hub() as hub:
            code, payload = self._import(self.package, "--expect-fingerprint", "")
            self.assertEqual(code, 2, payload)
            self.assertIn("64 hexadecimal", payload["errors"][0])
            self.assertFalse(SwarmNetwork.objects.exists())
            self.assertFalse((hub / "workspaces").exists())

    def test_import_of_a_missing_file_is_an_error(self):
        with self.other_hub() as hub:
            code, payload = self._cli(
                "import",
                "--project",
                str(self.project.identifier),
                "--name",
                "Imported",
                "--package",
                str(hub / "no-such-package.zip"),
            )
            self.assertEqual(code, 2, payload)
            self.assertIn("Could not read the startup package", payload["errors"][0])
            self.assertFalse(SwarmNetwork.objects.exists())

    def test_import_of_a_package_without_manifest_warns(self):
        with self.other_hub():
            code, payload = self._import(_without_manifests(self.package))
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["result"]["package_fingerprint"], "")
        self.assertEqual(len(payload["warnings"]), 1)
        self.assertIn("no manifest.json", payload["warnings"][0])


class WebTests(ProvisionedNetworkTestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)

    def _network_page(self):
        with patch("network.views._get_local_participant_status", return_value={}):
            response = self.client.get(reverse("network:network"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_export_shows_and_logs_the_fingerprint(self):
        UserCurrentNetwork.objects.create(user=self.user, network=self.network)
        with patch("network.views.get_logger") as get_logger:
            response = self.client.get(
                reverse("network:download_startup_kits", args=[self.network.identifier])
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.package)
        grouped = group_fingerprint(self.fingerprint)
        logged = [
            call.args[0] for call in get_logger.return_value.network.info.call_args_list
        ]
        self.assertTrue(
            logged[0].startswith("User exported startup kits for network 'Demo'")
        )
        self.assertIn(grouped, logged[1])
        self.assertIn(hashlib.sha256(self.package).hexdigest(), logged[1])

        page = self._network_page()
        self.assertIn(f"Network fingerprint: {grouped}", page)  # the message
        self.assertIn("text-green-800", _alert_classes(page, "Network fingerprint:"))
        self.assertIn('id="packageFingerprint"', page)
        self.assertIn(
            f'<span class="font-mono text-base font-semibold">{grouped}</span>', page
        )
        self.assertIn(self.root_ca, page)
        self.assertIn(
            "Compare this value with every other site by phone or video call, not "
            "through the channel that carried the package.",
            page,
        )

    def _post_import(self, package: bytes, expected=""):
        return self.client.post(
            reverse("network:new_network"),
            {
                "creation_method": "import",
                "title": "Imported",
                "description": "",
                "expected_fingerprint": expected,
                "startup_package": SimpleUploadedFile(
                    "package.zip", package, "application/zip"
                ),
            },
        )

    def test_import_form_checks_the_expected_fingerprint(self):
        with self.other_hub():
            response = self._post_import(
                self.package, group_fingerprint(self.fingerprint)
            )
            self.assertRedirects(
                response, reverse("network:network"), fetch_redirect_response=False
            )
            imported = SwarmNetwork.objects.get(identifier=self.network.identifier)
            self.assertEqual(imported.package_fingerprint, self.fingerprint)
            UserCurrentNetwork.objects.create(user=self.user, network=imported)
            page = self._network_page()
        self.assertIn(
            f"Network fingerprint: {group_fingerprint(self.fingerprint)}", page
        )
        self.assertIn("text-green-800", _alert_classes(page, "Network fingerprint:"))

    def test_import_form_refuses_another_fingerprint(self):
        with self.other_hub() as hub:
            response = self._post_import(self.package, "ab" * 32)
            self.assertRedirects(
                response, reverse("network:network"), fetch_redirect_response=False
            )
            self.assertFalse(SwarmNetwork.objects.exists())
            self.assertFalse((hub / "workspaces").exists())
            page = self._network_page()
        self.assertIn("not the expected", page)
        # Shown as an error, not as information.
        self.assertIn("text-red-800", _alert_classes(page, "not the expected"))

    def test_import_form_warns_about_a_package_without_manifest(self):
        with self.other_hub():
            self._post_import(_without_manifests(self.package))
            imported = SwarmNetwork.objects.get(identifier=self.network.identifier)
            UserCurrentNetwork.objects.create(user=self.user, network=imported)
            page = self._network_page()
        self.assertIn("The startup package has no manifest.json", page)
        self.assertIn("text-yellow-800", _alert_classes(page, "has no manifest.json"))
        self.assertIn("None: the package had no <code>manifest.json</code>", page)

    def test_before_an_export_the_page_says_where_the_fingerprint_appears(self):
        # A download with Kits leaves the page as it was.
        SwarmNetwork.objects.filter(pk=self.network.pk).update(
            package_fingerprint="", package_sha256=""
        )
        UserCurrentNetwork.objects.create(user=self.user, network=self.network)
        page = self._network_page()
        self.assertIn(
            "None yet: shown once the package has been exported. After downloading "
            "it with <strong>Kits</strong>, reload this page.",
            page,
        )

    def test_import_form_has_the_expected_fingerprint_field(self):
        response = self.client.get(reverse("network:new_network"))
        self.assertContains(response, 'name="expected_fingerprint"')


class ExportRefusalTests(NetworkFixture, TestCase):
    """An export whose kits disagree is refused (kits written by test_failover)."""

    def setUp(self):
        super().setUp()
        _add_flare_files(self.prod_dir)
        self.user.profile.accepted_terms = True
        self.user.profile.accepted_policy = True
        self.user.profile.save(update_fields=["accepted_terms", "accepted_policy"])

    def test_kits_naming_different_servers(self):
        (self.prod_dir / "node-B" / "startup" / "server_host.txt").write_text(
            "192.0.2.66"
        )

        with self.assertRaises(ValueError) as caught:
            network_services.export_startup_package(self.network)

        self.assertIn("node-B.zip/startup/server_host.txt", str(caught.exception))
        self.assertIn("was not exported", str(caught.exception))
        self.network.refresh_from_db()
        self.assertEqual(self.network.package_fingerprint, "")

    def test_a_server_participant_at_another_address(self):
        self.network.participants.filter(role="SERVER").update(ip="192.0.2.66")

        with self.assertRaises(ValueError) as caught:
            network_services.export_startup_package(self.network)

        self.assertIn(
            "SERVER participant the address 192.0.2.66", str(caught.exception)
        )

    def test_the_web_export_shows_why(self):
        (self.prod_dir / "node-B" / "startup" / "server_host.txt").write_text(
            "192.0.2.66"
        )
        UserCurrentProject.objects.create(user=self.user, project=self.project)
        client = Client()
        client.force_login(self.user)

        response = client.get(
            reverse("network:download_startup_kits", args=[self.network.identifier])
        )

        self.assertRedirects(
            response, reverse("network:network"), fetch_redirect_response=False
        )
        with patch("network.views._get_local_participant_status", return_value={}):
            page = client.get(reverse("network:network")).content.decode()
        self.assertIn("node-B.zip/startup/server_host.txt", page)
