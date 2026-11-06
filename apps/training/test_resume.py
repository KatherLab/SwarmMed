"""Resuming an interrupted swarm run from a checkpoint (training.resume)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import torch
from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings

from network.models import SwarmNetwork, SwarmParticipant
from project.models import Project

from . import services as training_services
from .models import TrainingJob
from .resume import (
    CHECKPOINT_FILE,
    COMPONENT_ID,
    COMPONENT_PATH,
    GLOBAL_MODEL_FILE,
    METADATA_FILE,
    RESUME_RECORD_FILE,
    START_CHECKPOINT_FILE,
    ResumeCheckpoint,
    ResumePlan,
    add_resume_checkpoint_component,
    find_resume_checkpoints,
    ship_resume_checkpoint,
)
from .utils import extract_total_rounds_from_flare_config

SWARM_CONTROLLER = "nvflare.app_common.ccwf.swarm_server_ctl.SwarmServerController"

TRAINING_SCRIPT = """\
import torch
import nvflare.client as flare

SWARM_ROUNDS = 4


def main():
    torch.manual_seed(0)
    model = torch.nn.Linear(4, 2)
    flare.init()
    site = flare.get_site_name()
    while flare.is_running():
        received = flare.receive()
        if received is None:
            break
        if received.params:
            model.load_state_dict(received.params)
        checksum = sum(float(t.double().sum()) for t in model.state_dict().values())
        print(
            f"SWARMMED_TEST site={site} round={received.current_round} "
            f"loaded={bool(received.params)} checksum={checksum:.6f}",
            flush=True,
        )
        with torch.no_grad():
            for parameter in model.parameters():
                parameter.add_(0.01)
        flare.send(
            flare.FLModel(
                params=model.state_dict(),
                metrics={"accuracy": 0.5},
                meta={"NUM_STEPS_CURRENT_ROUND": 1},
            )
        )


if __name__ == "__main__":
    main()
"""


def _write_round_start_checkpoint(app_dir: Path, next_round: int, site: str, value=1.0):
    app_dir.mkdir(parents=True, exist_ok=True)
    weights = {"fc.weight": torch.full((2, 4), value), "fc.bias": torch.zeros(2)}
    torch.save({"model": weights}, app_dir / CHECKPOINT_FILE)
    (app_dir / METADATA_FILE).write_text(
        json.dumps({"next_round": next_round, "site": site, "weights_format": "torch"})
    )
    return weights


def _write_global_model(app_dir: Path, log_text: str, value=2.0):
    app_dir.mkdir(parents=True, exist_ok=True)
    torch.save(
        {"model": {"fc.weight": torch.full((2, 4), value), "fc.bias": torch.ones(2)}},
        app_dir / GLOBAL_MODEL_FILE,
    )
    (app_dir.parent / "log.txt").write_text(log_text)


class FindResumeCheckpointsTests(SimpleTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.job = str(uuid.uuid4())

    def test_newest_round_wins_and_round_start_beats_same_round_global_model(self):
        _write_round_start_checkpoint(
            self.root / "node-A" / self.job / "app_node-A", 5, "node-A"
        )
        _write_round_start_checkpoint(
            self.root / "node-B" / self.job / "app_node-B", 4, "node-B"
        )
        _write_global_model(
            self.root / "node-B" / self.job / "app_node-B",
            "x - Saving result of round 2\ny - Saving result of round 4\n",
        )

        found = find_resume_checkpoints(self.root, self.job)

        self.assertEqual(
            [(c.site, c.source, c.next_round) for c in found],
            [
                ("node-A", "round-start", 5),
                ("node-B", "global-model", 5),
                ("node-B", "round-start", 4),
            ],
        )

    def test_final_model_round_comes_from_the_final_result_log_line(self):
        _write_global_model(
            self.root / "node-C" / self.job / "app_node-C",
            "Saving result of round 1\n"
            "Got last from client node-A with metric 0.5 at round 3\n",
        )

        (checkpoint,) = find_resume_checkpoints(self.root, self.job)

        self.assertEqual(checkpoint.next_round, 4)

    def test_global_model_without_log_has_unknown_round_and_ranks_last(self):
        _write_global_model(self.root / "node-C" / self.job / "app_node-C", "")
        _write_round_start_checkpoint(
            self.root / "node-A" / self.job / "app_node-A", 1, "node-A"
        )

        found = find_resume_checkpoints(self.root, self.job)

        self.assertEqual(found[0].next_round, 1)
        self.assertIsNone(found[-1].next_round)

    def test_other_jobs_are_ignored(self):
        _write_round_start_checkpoint(
            self.root / "node-A" / "other-job" / "app_node-A", 9, "node-A"
        )

        self.assertEqual(find_resume_checkpoints(self.root, self.job), [])


class TotalRoundsTests(SimpleTestCase):
    def test_resumed_job_counts_rounds_from_zero(self):
        config = {
            "workflows": [
                {
                    "id": "controller",
                    "path": SWARM_CONTROLLER,
                    "args": {"num_rounds": 12, "start_round": 8},
                }
            ]
        }
        self.assertEqual(extract_total_rounds_from_flare_config(config), 20)

    def test_new_job_is_unchanged(self):
        config = {
            "workflows": [
                {
                    "id": "controller",
                    "path": SWARM_CONTROLLER,
                    "args": {"num_rounds": 20},
                }
            ]
        }
        self.assertEqual(extract_total_rounds_from_flare_config(config), 20)


def _exported_job(root: Path, clients=("node-A", "node-B")) -> Path:
    job = root / "demo_job"
    deploy_map = {"app_server": ["server"]}
    for client in clients:
        config_dir = job / f"app_{client}" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "config_fed_client.json").write_text(
            json.dumps({"format_version": 2, "executors": [], "components": []})
        )
        deploy_map[f"app_{client}"] = [client]
    (job / "app_server" / "config").mkdir(parents=True)
    (job / "meta.json").write_text(
        json.dumps({"name": "demo_job", "deploy_map": deploy_map})
    )
    return job


class ExportedJobChangesTests(SimpleTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)

    def test_component_is_added_once_to_every_client_app(self):
        job = _exported_job(self.root)

        add_resume_checkpoint_component(job)
        add_resume_checkpoint_component(job)

        for client in ("node-A", "node-B"):
            config = json.loads(
                (job / f"app_{client}/config/config_fed_client.json").read_text()
            )
            entries = [c for c in config["components"] if c["id"] == COMPONENT_ID]
            self.assertEqual(
                entries, [{"id": COMPONENT_ID, "path": COMPONENT_PATH, "args": {}}]
            )
            self.assertTrue((job / f"app_{client}/custom/swarmmed_resume.py").exists())
        self.assertFalse((job / "app_server/custom/swarmmed_resume.py").exists())

    def test_checkpoint_goes_only_to_the_starting_client(self):
        job = _exported_job(self.root)
        weights = _write_round_start_checkpoint(
            self.root / "src", 3, "node-B", value=0.25
        )
        plan = ResumePlan(
            checkpoint=ResumeCheckpoint(
                path=str(self.root / "src" / CHECKPOINT_FILE),
                next_round=3,
                site="node-B",
                source="round-start",
            ),
            start_round=3,
            total_rounds=6,
            from_flare_job="old-job",
        )

        shipped = ship_resume_checkpoint(job, "node-A", plan)

        self.assertEqual(shipped, job / "app_node-A" / "custom" / START_CHECKPOINT_FILE)
        loaded = torch.load(shipped, weights_only=True)
        self.assertEqual(set(loaded), {"model"})
        self.assertTrue(torch.equal(loaded["model"]["fc.weight"], weights["fc.weight"]))
        environment = json.loads(
            (job / "app_node-A/config/environment.json").read_text()
        )
        self.assertEqual(environment, {"APP_CKPT": f"custom/{START_CHECKPOINT_FILE}"})
        record = json.loads(
            (job / "app_node-A/config" / RESUME_RECORD_FILE).read_text()
        )
        self.assertEqual(
            (record["start_round"], record["from_flare_job"]), (3, "old-job")
        )
        self.assertFalse((job / "app_node-B/config/environment.json").exists())
        self.assertFalse((job / "app_node-B/custom" / START_CHECKPOINT_FILE).exists())

    def test_checkpoint_must_be_plain_tensors(self):
        # Also a file found in this site's own job folders: code in the FL
        # client containers can write there.
        job = _exported_job(self.root)
        path = self.root / CHECKPOINT_FILE
        torch.save(
            {"model": {"w": torch.ones(1)}, "extra": object.__new__(_Opaque)}, path
        )
        plan = ResumePlan(
            checkpoint=ResumeCheckpoint(
                path=str(path), next_round=1, site="node-B", source="round-start"
            ),
            start_round=1,
            total_rounds=2,
        )

        with self.assertRaises(ValueError):
            ship_resume_checkpoint(job, "node-A", plan)
        self.assertFalse((job / "app_node-A/custom" / START_CHECKPOINT_FILE).exists())


class _Opaque:
    """A class torch's weights-only loader refuses."""


class SubmitHarness:
    """A running two-site network whose FL server is replaced by a fake session.

    ``self.submitted`` collects a copy of every job folder that was submitted.
    """

    clients = ("node-A", "node-B")

    def setUp(self):
        super().setUp()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.base_dir = Path(temp_dir.name)
        adapter = self.base_dir / "apps" / "training" / "flare_adapter.py"
        adapter.parent.mkdir(parents=True)
        shutil.copyfile(
            Path(settings.BASE_DIR) / "apps" / "training" / "flare_adapter.py", adapter
        )
        settings_override = override_settings(BASE_DIR=self.base_dir)
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        self.user = User.objects.create_user(
            username="resumer", password="test-password"
        )  # nosec B106
        self.project = Project.objects.create(title="Resume Project", author=self.user)
        self.network = SwarmNetwork.objects.create(
            name="Resume Network",
            project=self.project,
            author=self.user,
            status="RUNNING",
        )
        SwarmParticipant.objects.create(
            network=self.network, user=self.user, role="SERVER", participant_id="server"
        )
        for client in self.clients:
            SwarmParticipant.objects.create(
                network=self.network,
                user=self.user,
                role="CLIENT",
                participant_id=client,
            )
        self.network_dir = (
            self.base_dir
            / "workspaces"
            / str(self.project.identifier)
            / str(self.network.identifier)
        )
        self.network_dir.mkdir(parents=True)
        (self.network_dir / "runtime_requirements.txt").write_text("torch\n")
        admin_dir = self.network_dir / "admin"
        admin_dir.mkdir()

        self.submitted: list[Path] = []
        self.session = MagicMock()
        self.session.api.do_command.return_value = {}
        self.session.submit_job.side_effect = self._capture_submission

        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("SWARMMEDHUB_SWARM_ROUNDS", "SWARMMEDHUB_RESUME_CHECKPOINTS"):
            os.environ.pop(name, None)

        for target, value in {
            "resolve_admin_session_target": (
                "admin@nvidia.com",
                str(admin_dir),
                "127.0.0.1",
            ),
            "new_secure_session_with_host": self.session,
            "log_flare_pre_submit_diagnostics": None,
        }.items():
            patcher = patch.object(training_services, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        download = patch.object(
            training_services, "download_s3_folder", side_effect=self._fake_download
        )
        download.start()
        self.addCleanup(download.stop)

    def _fake_download(self, _bucket, _prefix, local_dir):
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        Path(local_dir, "training.py").write_text(TRAINING_SCRIPT)

    def _capture_submission(self, job_path):
        copy = self.base_dir / "submitted" / f"job{len(self.submitted)}"
        shutil.copytree(job_path, copy)
        self.submitted.append(copy)
        return str(uuid.uuid4())

    def client_workspace(self, client: str) -> Path:
        return self.network_dir / "workspace" / "resume_project" / "prod_00" / client


class SubmitWithResumeTests(SubmitHarness, TestCase):
    def _configs(self, job: Path):
        server = json.loads(
            (job / "app_server/config/config_fed_server.json").read_text()
        )
        clients = {
            client: json.loads(
                (job / f"app_{client}/config/config_fed_client.json").read_text()
            )
            for client in self.clients
        }
        return server, clients

    def test_new_job_gets_the_round_start_checkpoint_component(self):
        job = training_services.submit_training_job(
            actor=self.user, network=self.network
        )

        server, clients = self._configs(self.submitted[0])
        controller_args = server["workflows"][0]["args"]
        self.assertEqual(controller_args["num_rounds"], 4)
        self.assertNotIn("start_round", controller_args)
        for config in clients.values():
            self.assertIn(COMPONENT_ID, [c["id"] for c in config["components"]])
        self.assertEqual(job.total_rounds, 4)

    def test_component_can_be_turned_off(self):
        os.environ["SWARMMEDHUB_RESUME_CHECKPOINTS"] = "false"

        training_services.submit_training_job(actor=self.user, network=self.network)

        _server, clients = self._configs(self.submitted[0])
        for config in clients.values():
            self.assertNotIn(COMPONENT_ID, [c["id"] for c in config["components"]])

    def test_resumed_job_starts_at_the_checkpoint_round(self):
        source = self.base_dir / "ckpt"
        _write_round_start_checkpoint(source, 3, "node-B")
        plan = ResumePlan(
            checkpoint=ResumeCheckpoint(
                path=str(source / CHECKPOINT_FILE),
                next_round=3,
                site="node-B",
                source="round-start",
            ),
            start_round=3,
            total_rounds=10,
        )

        job = training_services.submit_training_job(
            actor=self.user, network=self.network, resume=plan
        )

        exported = self.submitted[0]
        server, clients = self._configs(exported)
        controller_args = server["workflows"][0]["args"]
        self.assertEqual(
            (controller_args["start_round"], controller_args["num_rounds"]), (3, 7)
        )
        starting = controller_args["starting_client"]
        persistor = next(
            c for c in clients[starting]["components"] if c["id"] == "persistor"
        )
        self.assertEqual(persistor["args"], {"allow_numpy_conversion": False})
        self.assertTrue(
            (exported / f"app_{starting}/custom" / START_CHECKPOINT_FILE).exists()
        )
        self.assertEqual(job.total_rounds, 10)

    def test_numpy_jobs_cannot_be_resumed(self):
        plan = ResumePlan(
            checkpoint=ResumeCheckpoint(
                path="x", next_round=1, site="", source="given"
            ),
            start_round=1,
            total_rounds=2,
        )
        with (
            patch.object(
                training_services,
                "download_s3_folder",
                side_effect=lambda _b, _p, d: Path(d, "training.py").write_text(
                    "import numpy\n"
                ),
            ),
            self.assertRaisesRegex(ValueError, "Only PyTorch"),
        ):
            training_services.submit_training_job(
                actor=self.user, network=self.network, resume=plan
            )
        self.assertEqual(self.submitted, [])

    def test_nothing_left_to_resume_is_rejected_before_submission(self):
        plan = ResumePlan(
            checkpoint=ResumeCheckpoint(
                path="x", next_round=4, site="", source="given"
            ),
            start_round=4,
        )
        with self.assertRaisesRegex(ValueError, "Nothing to resume"):
            training_services.submit_training_job(
                actor=self.user, network=self.network, resume=plan
            )
        self.assertEqual(TrainingJob.objects.count(), 0)


class ResumeTrainingJobTests(SubmitHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.old_uuid = str(uuid.uuid4())
        self.old_job = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id=self.old_uuid,
            flare_job_uuid=self.old_uuid,
            total_rounds=8,
        )

    def _checkpoint_at(self, client: str, next_round: int):
        app_dir = self.client_workspace(client) / self.old_uuid / f"app_{client}"
        return _write_round_start_checkpoint(app_dir, next_round, client)

    def test_resumes_from_the_newest_checkpoint_and_fails_the_old_job(self):
        self._checkpoint_at("node-A", 2)
        self._checkpoint_at("node-B", 3)

        with patch.object(
            training_services, "_job_active_on_current_server", return_value=False
        ):
            new_job = training_services.resume_training_job(
                actor=self.user, network=self.network
            )

        server = json.loads(
            (self.submitted[0] / "app_server/config/config_fed_server.json").read_text()
        )
        args = server["workflows"][0]["args"]
        self.assertEqual((args["start_round"], args["num_rounds"]), (3, 5))
        self.old_job.refresh_from_db()
        self.assertEqual(self.old_job.status, "FAILED")
        self.assertEqual(new_job.status, "RUNNING")
        self.assertEqual(new_job.total_rounds, 8)
        records = json.loads(
            (self.network_dir / "job" / "resumed_jobs.json").read_text()
        )
        self.assertEqual(records[0]["from_flare_job"], self.old_uuid)
        self.assertEqual(records[0]["checkpoint"]["site"], "node-B")

    def test_refuses_while_the_server_still_runs_the_job(self):
        self._checkpoint_at("node-A", 2)

        with (
            patch.object(
                training_services, "_job_active_on_current_server", return_value=True
            ),
            self.assertRaisesRegex(ValueError, "still active"),
        ):
            training_services.resume_training_job(actor=self.user, network=self.network)
        self.assertEqual(self.submitted, [])
        self.old_job.refresh_from_db()
        self.assertEqual(self.old_job.status, "RUNNING")

    def test_without_checkpoint_points_to_the_other_sites(self):
        with self.assertRaisesRegex(LookupError, "training checkpoints"):
            training_services.resume_training_job(actor=self.user, network=self.network)

    def test_given_checkpoint_needs_a_round(self):
        path = self.base_dir / "elsewhere.pt"
        torch.save({"model": {"w": torch.ones(1)}}, path)

        with self.assertRaisesRegex(ValueError, "--start-round"):
            training_services.resume_training_job(
                actor=self.user, network=self.network, checkpoint_path=str(path)
            )

    def test_active_check_reads_the_server_job_list(self):
        self.session.api.do_command.return_value = {
            "details": [{"job_id": self.old_uuid, "status": "RUNNING"}]
        }
        with patch.object(
            training_services,
            "parse_nvflare_jobs",
            return_value=[{"job_id": self.old_uuid, "status": "RUNNING"}],
        ):
            self.assertTrue(
                training_services._job_active_on_current_server(
                    self.network, self.old_job
                )
            )
        with patch.object(
            training_services,
            "parse_nvflare_jobs",
            return_value=[{"job_id": self.old_uuid, "status": "FINISHED:ABANDONED"}],
        ):
            self.assertFalse(
                training_services._job_active_on_current_server(
                    self.network, self.old_job
                )
            )
