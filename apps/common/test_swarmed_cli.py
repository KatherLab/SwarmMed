"""Tests for the local swarmed CLI."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from data.models import ValidationRun
from network.models import SwarmNetwork, SwarmParticipant
from project.models import Project, UserCurrentProject
from results.models import ResultsVisualizationRun
from training.models import TrainingJob

from swarmed_cli.main import build_parser, main
from swarmed_cli.support import CLIState, resolve_actor, resolve_project


class CLIParserTests(SimpleTestCase):
    """Pure argparse coverage for the CLI tree."""

    def test_parser_registers_project_create_command(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "project",
                "create",
                "--title",
                "Demo",
                "--code-dir",
                "/tmp/code",
            ]
        )
        self.assertEqual(args.handler, "project_create")
        self.assertEqual(args.command_name, "project create")

    def test_parser_accepts_json_after_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["project", "list", "--json"])
        self.assertTrue(args.json)
        self.assertEqual(args.handler, "project_list")

    def test_parser_preserves_top_level_common_options(self):
        parser = build_parser()
        args = parser.parse_args(["--json", "--user", "alice", "project", "list"])
        self.assertTrue(args.json)
        self.assertEqual(args.user, "alice")
        self.assertEqual(args.handler, "project_list")

    def test_parser_accepts_no_local_client_for_real_network(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "network",
                "create",
                "--name",
                "DUKE IID 3-site",
                "--participant",
                "node_A=100.64.4.100",
                "--no-local-client",
            ]
        )

        self.assertTrue(args.no_local_client)
        self.assertEqual(args.handler, "network_create")

    def test_main_returns_usage_code_for_invalid_command(self):
        stream = StringIO()
        with redirect_stdout(stream), redirect_stderr(stream):
            code = main(["project", "missing-subcommand"])
        self.assertEqual(code, 2)

    def test_main_returns_json_envelope_for_invalid_json_command(self):
        stdout = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stdout):
            code = main(["project", "missing-subcommand", "--json"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "swarmed")
        self.assertTrue(payload["errors"])


class CLIStateResolutionTests(TestCase):
    """Context and output boundary tests for the CLI."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="alice", password="test-password"
        )  # nosec B106
        self.project = Project.objects.create(
            title="Project Alpha",
            author=self.user,
        )
        UserCurrentProject.objects.create(user=self.user, project=self.project)

    def _run_cli(self, *argv):
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(list(argv))
        return code, stdout.getvalue()

    def test_resolve_actor_prefers_explicit_user(self):
        actor = resolve_actor("alice")
        self.assertEqual(actor.id, self.user.id)

    def test_resolve_actor_uses_env_before_os_username(self):
        other_user = User.objects.create_user(
            username="from-env", password="test-password"
        )  # nosec B106
        with patch.dict(os.environ, {"SWARMED_USER": "from-env"}, clear=False):
            with patch("swarmed_cli.support.getpass.getuser", return_value="ignored"):
                actor = resolve_actor()
        self.assertEqual(actor.id, other_user.id)

    def test_resolve_project_uses_current_project(self):
        state = CLIState(user=self.user, json_output=False)
        project = resolve_project(state)
        self.assertEqual(project.id, self.project.id)

    def test_json_success_output_uses_envelope(self):
        code, output = self._run_cli("project", "list", "--user", "alice", "--json")
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "project list")
        self.assertIn("projects", payload["result"])
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["errors"], [])

    def test_top_level_common_options_work_with_subcommands(self):
        code, output = self._run_cli("--json", "--user", "alice", "project", "list")
        payload = json.loads(output)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "project list")

    def test_json_error_output_uses_envelope_and_exit_code_2(self):
        UserCurrentProject.objects.filter(user=self.user).delete()
        code, output = self._run_cli("data", "ls", "--user", "alice", "--json")
        payload = json.loads(output)
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "data ls")
        self.assertTrue(payload["errors"])

    def test_json_mode_keeps_handler_stdout_out_of_payload(self):
        network = SwarmNetwork.objects.create(
            name="Noisy Network",
            project=self.project,
            author=self.user,
            status="RUNNING",
        )
        job = TrainingJob.objects.create(
            project=self.project,
            network=network,
            status="RUNNING",
            flare_job_id="noisy-job",
        )

        def noisy_submit(*_args, **_kwargs):
            print("handler noise")
            return job

        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch(
            "training.services.submit_training_job", side_effect=noisy_submit
        ), patch(
            "training.services.serialize_training_job",
            return_value={"identifier": str(job.identifier)},
        ):
            code = main(
                [
                    "training",
                    "start",
                    "--user",
                    self.user.username,
                    "--network",
                    str(network.identifier),
                    "--json",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertNotIn("handler noise", stdout.getvalue())
        self.assertIn("handler noise", stderr.getvalue())


class CLISharedPathTests(TestCase):
    """Tests that the UI and CLI route through the same service functions."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="devuser", password="test-password"
        )  # nosec B106
        self.project = Project.objects.create(
            title="Shared Path Project",
            author=self.user,
        )
        self.network = SwarmNetwork.objects.create(
            name="Shared Network",
            project=self.project,
            author=self.user,
            status="RUNNING",
        )
        self.validation_run = ValidationRun.objects.create(
            project=self.project, user=self.user, celery_task_id="validation-task"
        )
        self.training_job = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id="job-uuid",
        )
        self.visualization_run = ResultsVisualizationRun.objects.create(
            project=self.project,
            job=self.training_job,
            user=self.user,
            flare_job_id="job-uuid",
            celery_task_id="results-task",
        )
        UserCurrentProject.objects.create(user=self.user, project=self.project)

    def _run_cli(self, *argv):
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(list(argv))
        return code, stdout.getvalue()

    def test_project_create_ui_and_cli_share_service(self):
        from project import views as project_views

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = Path(tmpdir)
            (code_dir / "training.py").write_text("print('hello')\n")
            with patch(
                "project.services.create_project", return_value=self.project
            ) as mocked:
                request = self.factory.post(
                    reverse("project:project_create"),
                    {"title": "Shared Path Project", "description": ""},
                )
                request.user = self.user
                response = project_views.project_create(request)
                self.assertEqual(response.status_code, 302)

                code, _ = self._run_cli(
                    "project",
                    "create",
                    "--user",
                    self.user.username,
                    "--title",
                    "CLI Project",
                    "--code-dir",
                    str(code_dir),
                )
                self.assertEqual(code, 0)
                self.assertEqual(mocked.call_count, 2)

    def test_data_validate_ui_and_cli_share_service(self):
        from data import views as data_views

        with patch(
            "data.services.start_validation", return_value=self.validation_run
        ) as mocked:
            request = self.factory.post(reverse("data:start_validation"))
            request.user = self.user
            response = data_views.start_validation(request)
            self.assertEqual(response.status_code, 200)

            code, _ = self._run_cli(
                "data",
                "validate",
                "--user",
                self.user.username,
                "--project",
                str(self.project.identifier),
            )
            self.assertEqual(code, 0)
            self.assertEqual(mocked.call_count, 2)

    def test_network_create_ui_and_cli_share_service(self):
        from network import views as network_views

        provisioned_network = SwarmNetwork.objects.create(
            name="Provisioned Network",
            project=self.project,
            author=self.user,
            status="PROVISIONED",
        )
        with patch(
            "network.services.create_local_test_network",
            return_value=provisioned_network,
        ) as mocked:
            request = self.factory.post(
                reverse("network:new_network"),
                {
                    "creation_method": "local_test",
                    "title": "Local Test Network",
                    "description": "",
                },
            )
            request.user = self.user
            response = network_views.new_network(request)
            self.assertEqual(response.status_code, 302)

            code, _ = self._run_cli(
                "network",
                "create",
                "--user",
                self.user.username,
                "--project",
                str(self.project.identifier),
                "--name",
                "CLI Local Test",
                "--local-test",
            )
            self.assertEqual(code, 0)
            self.assertEqual(mocked.call_count, 2)

    def test_network_create_no_local_client_records_only_explicit_clients(self):
        with patch("network.services.generate_flare_startup_kit"), patch(
            "network.services.get_hostname", return_value="cosmos"
        ), patch("network.services.get_tailscale_ip", return_value="100.64.4.104"):
            code, output = self._run_cli(
                "network",
                "create",
                "--user",
                self.user.username,
                "--project",
                str(self.project.identifier),
                "--json",
                "--name",
                "DUKE IID 3-site",
                "--server-ip",
                "100.64.4.104",
                "--participant",
                "node_A=100.64.4.100",
                "--participant",
                "node_B=100.64.4.102",
                "--participant",
                "node_C=100.64.4.103",
                "--no-local-client",
            )

        self.assertEqual(code, 0)
        network_id = json.loads(output)["result"]["identifier"]
        participants = SwarmParticipant.objects.filter(
            network__identifier=network_id
        ).order_by("role", "participant_id")
        client_names = sorted(
            p.participant_id for p in participants if p.role == "CLIENT"
        )

        # Per the Hub/CLI parity plan, participant names submitted from the
        # CLI are normalised through ``safe_participant_name`` exactly once
        # so provisioning, training submission, gossip, and tests all use the
        # same FLARE-safe form. ``node_A`` therefore lands as ``node-A``.
        self.assertEqual(client_names, ["node-A", "node-B", "node-C"])
        self.assertNotIn("cosmos", client_names)

    def test_training_start_ui_and_cli_share_service(self):
        from training import views as training_views

        with patch(
            "training.services.submit_training_job", return_value=self.training_job
        ) as submit_mock, patch(
            "training.services.serialize_training_job",
            return_value={"identifier": str(self.training_job.identifier)},
        ):
            request = self.factory.get(
                reverse(
                    "training:start_training",
                    kwargs={"network_id": self.network.identifier},
                )
            )
            request.user = self.user
            request.session = {}
            with patch("training.views.messages.success"), patch(
                "training.views.messages.error"
            ):
                response = training_views.start_training(
                    request, self.network.identifier
                )
            self.assertEqual(response.status_code, 302)

            code, _ = self._run_cli(
                "training",
                "start",
                "--user",
                self.user.username,
                "--network",
                str(self.network.identifier),
            )
            self.assertEqual(code, 0)
            self.assertEqual(submit_mock.call_count, 2)

    def test_results_visualize_ui_and_cli_share_service(self):
        from results import views as results_views

        with patch(
            "results.services.start_results_visualization",
            return_value=self.visualization_run,
        ) as mocked:
            request = self.factory.post(
                reverse(
                    "results:start_results_visualization",
                    kwargs={"job_id": str(self.training_job.identifier)},
                )
            )
            request.user = self.user
            response = results_views.start_results_visualization(
                request, str(self.training_job.identifier)
            )
            self.assertEqual(response.status_code, 200)

            code, _ = self._run_cli(
                "results",
                "visualize",
                "--user",
                self.user.username,
                "--project",
                str(self.project.identifier),
                "--job",
                str(self.training_job.identifier),
            )
            self.assertEqual(code, 0)
            self.assertEqual(mocked.call_count, 2)


class CLIHappyPathTests(TestCase):
    """A mocked end-to-end CLI happy path."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="runner", password="test-password"
        )  # nosec B106
        self.storage_dir = tempfile.TemporaryDirectory()
        self.code_dir = tempfile.TemporaryDirectory()
        self.data_dir = tempfile.TemporaryDirectory()
        self.download_dir = tempfile.TemporaryDirectory()
        self.settings_override = self.settings(
            STORAGES={
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": self.storage_dir.name},
                },
                "staticfiles": {
                    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
                },
            },
            DEFAULT_FILE_STORAGE="django.core.files.storage.FileSystemStorage",
        )
        self.settings_override.enable()

        code_root = Path(self.code_dir.name)
        (code_root / "training.py").write_text(
            "def main(project_id='default_project'):\n    return project_id\n"
        )
        data_root = Path(self.data_dir.name)
        (data_root / "dataset.csv").write_text("value\n1\n")

    def tearDown(self):
        self.settings_override.disable()
        self.storage_dir.cleanup()
        self.code_dir.cleanup()
        self.data_dir.cleanup()
        self.download_dir.cleanup()

    def _run_cli(self, *argv):
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(list(argv))
        return code, stdout.getvalue()

    def test_mocked_happy_path_commands(self):
        code, create_output = self._run_cli(
            "project",
            "create",
            "--user",
            self.user.username,
            "--json",
            "--title",
            "CLI Flow",
            "--code-dir",
            self.code_dir.name,
        )
        self.assertEqual(code, 0)
        created_project = json.loads(create_output)["result"]
        project_id = created_project["identifier"]

        code, _ = self._run_cli(
            "project",
            "use",
            "--user",
            self.user.username,
            project_id,
        )
        self.assertEqual(code, 0)

        code, upload_output = self._run_cli(
            "data",
            "upload",
            "--user",
            self.user.username,
            "--project",
            project_id,
            "--json",
            "--dest",
            "incoming",
            str(Path(self.data_dir.name) / "dataset.csv"),
        )
        self.assertEqual(code, 0)
        upload_payload = json.loads(upload_output)
        self.assertTrue(upload_payload["result"]["saved"])

        with patch("network.services.generate_flare_startup_kit"):
            code, network_output = self._run_cli(
                "network",
                "create",
                "--user",
                self.user.username,
                "--project",
                project_id,
                "--json",
                "--name",
                "Flow Network",
                "--local-test",
            )
        self.assertEqual(code, 0)
        network_id = json.loads(network_output)["result"]["identifier"]

        flow_network = SwarmNetwork.objects.get(identifier=network_id)
        flow_network.status = "RUNNING"
        flow_network.save(update_fields=["status"])

        training_job = TrainingJob.objects.create(
            project=flow_network.project,
            network=flow_network,
            status="RUNNING",
            flare_job_id="flow-job",
        )

        with patch(
            "training.services.submit_training_job", return_value=training_job
        ), patch(
            "training.services.serialize_training_job",
            return_value={"identifier": str(training_job.identifier)},
        ):
            code, _ = self._run_cli(
                "training",
                "start",
                "--user",
                self.user.username,
                "--network",
                network_id,
            )
        self.assertEqual(code, 0)

        with patch("results.services.sync_results", return_value=None):
            code, _ = self._run_cli(
                "results",
                "sync",
                "--user",
                self.user.username,
                "--project",
                project_id,
            )
        self.assertEqual(code, 0)

        with patch(
            "results.services.download_results_to_directory",
            return_value={
                "project_identifier": project_id,
                "job_identifier": str(training_job.identifier),
                "output_dir": self.download_dir.name,
                "downloaded_files": [str(Path(self.download_dir.name) / "model.pt")],
            },
        ):
            code, _ = self._run_cli(
                "results",
                "download",
                "--user",
                self.user.username,
                "--project",
                project_id,
                "--job",
                str(training_job.identifier),
                "--out",
                self.download_dir.name,
            )
        self.assertEqual(code, 0)
