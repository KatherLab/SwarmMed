"""Resuming an interrupted swarm run from a checkpoint (training.resume)."""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import torch
from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from swarmed_cli.main import main

from network import utils as network_utils
from network.models import SwarmNetwork, SwarmParticipant
from network.services import set_current_network
from project.models import Project

from . import resume as training_resume
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
    describe_given_checkpoint,
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


# JSON that Python cannot parse: json.loads raises RecursionError, not ValueError.
NESTED_JSON = "[" * 100_000 + "]" * 100_000


def _write_round_start_checkpoint(
    app_dir: Path,
    next_round: int,
    site: str,
    value=1.0,
    *,
    total_rounds: int | None = 8,
    job_id: str | None = None,
    network: str | None = None,
    saved_at: float | None = None,
):
    """Write a round-start checkpoint as the saver inside a job writes it.

    The JSON and the metadata in the .pt name the run's total, as in a job
    submitted by SwarmMed 1.0.6 or later; ``total_rounds=None`` writes them as
    earlier releases did. The job is the folder's unless ``job_id`` names one.
    """
    app_dir.mkdir(parents=True, exist_ok=True)
    weights = {"fc.weight": torch.full((2, 4), value), "fc.bias": torch.zeros(2)}
    metadata = {
        "next_round": next_round,
        "job_id": app_dir.parent.name if job_id is None else job_id,
        "site": site,
        "weights_format": "torch",
        "num_tensors": len(weights),
        "saved_at": time.time() if saved_at is None else saved_at,
    }
    if total_rounds is not None:
        metadata.update(total_rounds=total_rounds, start_round=0)
        if network is not None:
            metadata["network"] = network
    torch.save(
        {"model": weights, "swarmmed_resume": metadata}, app_dir / CHECKPOINT_FILE
    )
    (app_dir / METADATA_FILE).write_text(json.dumps(metadata))
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

    def test_metadata_comes_from_the_pt_when_the_json_is_missing(self):
        app_dir = self.root / "node-B" / self.job / "app_node-B"
        _write_round_start_checkpoint(app_dir, 3, "node-B")
        (app_dir / METADATA_FILE).unlink()

        checkpoint = describe_given_checkpoint(str(app_dir / CHECKPOINT_FILE), None)

        self.assertEqual(
            (
                checkpoint.next_round,
                checkpoint.site,
                getattr(checkpoint, "job_id", None),
                checkpoint.total_rounds,
            ),
            (3, "node-B", self.job, 8),
        )

    def test_metadata_keeps_only_valid_values(self):
        # Code in the FL client containers can write the checkpoint and its JSON.
        app_dir = self.root / "node-B" / self.job / "app_node-B"
        _write_round_start_checkpoint(app_dir, 3, "node-B")
        (app_dir / METADATA_FILE).write_text(
            json.dumps(
                {
                    "next_round": -1,
                    "job_id": "../../x",
                    "network": self.job.upper(),
                    "site": "../node-A",
                    "total_rounds": "8",
                    "weights_format": "pickle",
                    "saved_at": "yesterday",
                }
            )
        )

        metadata = training_resume.checkpoint_metadata(app_dir / CHECKPOINT_FILE)
        (checkpoint,) = find_resume_checkpoints(self.root, self.job)

        self.assertEqual(metadata, {})
        self.assertEqual(
            (
                checkpoint.next_round,
                checkpoint.site,
                checkpoint.job_id,
                checkpoint.network,
                checkpoint.total_rounds,
                checkpoint.weights_format,
            ),
            # The site and the job are the folders'.
            (None, "node-B", self.job, "", None, "torch"),
        )

    def test_a_checkpoint_total_beyond_any_run_counts_as_none(self):
        app_dir = self.root / "node-B" / self.job / "app_node-B"
        _write_round_start_checkpoint(app_dir, 3, "node-B", total_rounds=3_000_000_000)

        (checkpoint,) = find_resume_checkpoints(self.root, self.job)

        self.assertEqual((checkpoint.next_round, checkpoint.total_rounds), (3, None))

    def test_a_json_nested_too_deeply_gives_way_to_the_pt(self):
        app_dir = self.root / "study" / "prod_00" / "node-B" / self.job / "app_node-B"
        _write_round_start_checkpoint(app_dir, 3, "node-B")
        (app_dir / METADATA_FILE).write_text(NESTED_JSON)

        (checkpoint,) = find_resume_checkpoints(self.root, self.job)

        self.assertEqual((checkpoint.next_round, checkpoint.total_rounds), (3, 8))
        self.assertEqual(training_resume.newest_round_start_job(self.root), self.job)


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


def _write_job_file(root: Path, participant: str, job: str, relative: str, content):
    """Write a file of a job as FLARE deploys the job at a site."""
    path = root / "study" / "prod_00" / participant / job / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def _swarm_server_config(*, num_rounds: int, start_round: int = 0) -> dict:
    return {
        "workflows": [
            {
                "id": "controller",
                "path": SWARM_CONTROLLER,
                "args": {"num_rounds": num_rounds, "start_round": start_round},
            }
        ]
    }


class JobFoldersTests(SimpleTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.job = str(uuid.uuid4())

    def test_a_job_id_or_its_prefix_finds_the_jobs_folders(self):
        prod_dir = self.root / "study" / "prod_00"
        expected = sorted(
            [
                prod_dir / self.job,
                prod_dir / "node-A" / self.job,
                prod_dir / "server" / self.job,
            ]
        )
        for folder in expected:
            folder.mkdir(parents=True)
        # A deeper folder, a job whose ID differs in the eighth character, and a
        # folder whose name only starts with the job's ID.
        (prod_dir / "node-A" / self.job / "app_node-A" / self.job).mkdir(parents=True)
        other = self.job[:7] + ("1" if self.job[7] == "0" else "0") + self.job[8:]
        (prod_dir / "node-A" / other).mkdir()
        (prod_dir / "node-A" / f"{self.job}.old").mkdir()

        for identifier in (self.job, self.job.upper(), self.job[:8]):
            with self.subTest(identifier=identifier):
                self.assertEqual(
                    sorted(training_resume.job_folders(self.root, identifier)),
                    expected,
                )

    def test_anything_but_a_job_id_is_not_looked_up(self):
        (self.root / "study" / "prod_00" / "node-A" / self.job).mkdir(parents=True)

        with patch.object(Path, "glob", side_effect=AssertionError("looked up")):
            for identifier in (
                "",
                "../../x",
                "*",
                "[0-9a-f]*",
                self.job[:7],
                f"{self.job}/..",
                f"-{self.job[:8]}",
                f"{self.job}0",
            ):
                with self.subTest(identifier=identifier):
                    self.assertEqual(
                        training_resume.job_folders(self.root, identifier), []
                    )


class ReadRunPlanTests(SimpleTestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        self.job = str(uuid.uuid4())

    def _write(self, participant, relative, content, *, job=None) -> str:
        job = job or self.job
        path = _write_job_file(self.root, participant, job, relative, content)
        return str(path.relative_to(self.root))

    def _plan(self):
        plan = training_resume.read_run_plan(self.root, self.job)
        return plan and (plan.total_rounds, plan.start_round, plan.origin)

    def test_the_run_file_comes_first_then_the_server_config_then_a_resume_record(self):
        record = self._write(
            "node-A",
            "app_node-A/config/swarmmed_resume.json",
            {"start_round": 3, "total_rounds": 40},
        )
        self.assertEqual(self._plan(), (40, 3, record))

        server_config = self._write(
            "server",
            "app_server/config/config_fed_server.json",
            _swarm_server_config(num_rounds=27, start_round=3),
        )
        self.assertEqual(self._plan(), (30, 3, server_config))

        run_file = self._write(
            "node-A",
            "app_node-A/config/swarmmed_run.json",
            {"total_rounds": 8, "start_round": 3},
        )
        self.assertEqual(self._plan(), (8, 3, run_file))

    def test_only_whole_rounds_in_range_from_the_jobs_own_folders_count(self):
        # Code in the FL client containers can write these files.
        self._write(
            "node-A",
            "app_node-A/config/swarmmed_run.json",
            {"total_rounds": 30, "start_round": 0},
            job=str(uuid.uuid4()),
        )
        for content in (
            {"total_rounds": 5, "start_round": 5},
            {"total_rounds": 5, "start_round": -1},
            {"total_rounds": "8", "start_round": 0},
            {"total_rounds": True, "start_round": 0},
            {"total_rounds": 8, "start_round": 2.0},
            {"total_rounds": 8},
            [8, 3],
            "{not json",
        ):
            with self.subTest(content=content):
                self._write("node-A", "app_node-A/config/swarmmed_run.json", content)
                self.assertIsNone(self._plan())

    def test_a_total_beyond_any_run_counts_as_none(self):
        # A job record holds at most 2**31 - 1 rounds; no run has 100000.
        self._write(
            "node-A",
            "app_node-A/config/swarmmed_run.json",
            {"total_rounds": 3_000_000_000, "start_round": 0},
        )
        server_config = self._write(
            "server",
            "app_server/config/config_fed_server.json",
            _swarm_server_config(num_rounds=5),
        )

        self.assertEqual(self._plan(), (5, 0, server_config))

    def test_a_file_nested_too_deeply_counts_as_no_data(self):
        self._write("node-A", "app_node-A/config/swarmmed_run.json", NESTED_JSON)
        server_config = self._write(
            "server",
            "app_server/config/config_fed_server.json",
            _swarm_server_config(num_rounds=5),
        )

        self.assertEqual(self._plan(), (5, 0, server_config))


class ResumeCheckpointSaverTests(SimpleTestCase):
    """The round-start checkpoint as the saver writes it inside a FLARE job."""

    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.app_root = Path(temp_dir.name) / "app_node-A"
        (self.app_root / "config").mkdir(parents=True)
        self.job = str(uuid.uuid4())
        self.network = str(uuid.uuid4())

    def _save_round_start(self, current_round=3):
        from nvflare.apis.fl_constant import FLContextKey, ReservedKey
        from nvflare.apis.fl_context import FLContext
        from nvflare.app_common.abstract.model import ModelLearnableKey
        from nvflare.app_common.app_constant import AppConstants

        from .swarmmed_resume import ResumeCheckpointSaver

        fl_ctx = FLContext()
        for key, value in {
            AppConstants.GLOBAL_MODEL: {
                ModelLearnableKey.WEIGHTS: {"fc.weight": torch.ones(2, 4)}
            },
            AppConstants.CURRENT_ROUND: current_round,
            FLContextKey.APP_ROOT: str(self.app_root),
            ReservedKey.RUN_NUM: self.job,
            ReservedKey.IDENTITY_NAME: "node-A",
        }.items():
            fl_ctx.set_prop(key, value, private=True, sticky=False)

        ResumeCheckpointSaver()._save(fl_ctx)

        written = json.loads((self.app_root / METADATA_FILE).read_text())
        embedded = torch.load(self.app_root / CHECKPOINT_FILE, weights_only=True)
        self.assertEqual(embedded["swarmmed_resume"], written)
        self.assertIsInstance(written.pop("saved_at"), float)
        return written

    def test_checkpoint_records_the_run_total_and_network(self):
        (self.app_root / "config" / "swarmmed_run.json").write_text(
            json.dumps(
                {
                    "format": 1,
                    "network": self.network,
                    "total_rounds": 40,
                    "start_round": 0,
                    "num_rounds": 40,
                    "from_flare_job": "",
                }
            )
        )

        self.assertEqual(
            self._save_round_start(),
            {
                "next_round": 3,
                "job_id": self.job,
                "site": "node-A",
                "weights_format": "torch",
                "num_tensors": 1,
                "total_rounds": 40,
                "start_round": 0,
                "network": self.network,
            },
        )

    def test_checkpoint_without_run_file_keeps_the_old_keys(self):
        # A job submitted before the run file existed, or a file it cannot read.
        for content in (None, "{not json", json.dumps(["not", "a", "plan"])):
            with self.subTest(content=content):
                run_file = self.app_root / "config" / "swarmmed_run.json"
                if content is not None:
                    run_file.write_text(content)

                self.assertEqual(
                    self._save_round_start(),
                    {
                        "next_round": 3,
                        "job_id": self.job,
                        "site": "node-A",
                        "weights_format": "torch",
                        "num_tensors": 1,
                    },
                )


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


def flare_list_jobs_reply(jobs: list[dict]) -> dict:
    """An FL server's reply to ``list_jobs`` as NVIDIA FLARE 2.7.1's admin API
    returns it: the jobs in ``meta["jobs"]``, a table for display in ``data``.

    FlareListJobsReplyTests compares it with the reply that FLARE's own code makes.
    """
    rows = [[job["job_id"], "swarm_job", job["status"]] for job in jobs]
    return {
        "time": "2026-01-01 00:00:00.000000",
        "data": [
            {"type": "table", "rows": [["JOB ID", "NAME", "STATUS"], *rows]}
            if jobs
            else {"type": "string", "data": "No jobs found."},
            {"type": "success", "data": ""},
        ],
        "meta": {"jobs": [dict(job) for job in jobs], "status": "ok", "info": ""},
        "status": "SUCCESS",
    }


def flare_admin_api(
    jobs: dict[str, str | dict] | None = None,
    *,
    answers: bool = True,
    store_error: Exception | None = None,
):
    """NVIDIA FLARE 2.7.1's admin API client, connected to a stand-in FL server.

    The server runs FLARE's own ``list_jobs`` command over a job store that holds
    ``jobs`` (job ID -> status, or -> fields of the job's meta), and the client's
    own processing turns the server's answer into what ``do_command`` returns.
    Only the network connection is replaced; with ``answers=False`` the server
    does not answer in time, and with ``store_error`` reading the job store
    raises that error.
    """
    from nvflare.apis.job_def import Job
    from nvflare.apis.job_def_manager_spec import JobDefManagerSpec
    from nvflare.fuel.hci.client.api import AdminAPI
    from nvflare.fuel.hci.conn import Connection
    from nvflare.fuel.hci.proto import validate_proto
    from nvflare.fuel.hci.server.reg import ServerCommandRegister
    from nvflare.private.fed.server.job_cmds import JobCommandModule

    submitted = datetime.datetime.now()
    store = [
        Job(
            job_id=job_id,
            resource_spec={},
            deploy_map={},
            meta={
                "job_id": job_id,
                "name": "swarm_job",
                "submit_time": submitted.timestamp(),
                "submit_time_iso": submitted.isoformat(),
                "start_time": submitted.strftime("%Y-%m-%d %H:%M:%S.%f"),
                **(job if isinstance(job, dict) else {"status": job}),
            },
        )
        for job_id, job in (jobs or {}).items()
    ]
    engine = MagicMock()
    engine.job_def_manager = MagicMock(spec=JobDefManagerSpec)
    engine.job_def_manager.get_all_jobs.return_value = store
    engine.job_def_manager.get_all_jobs.side_effect = store_error
    engine.new_context.side_effect = lambda: contextlib.nullcontext(MagicMock())
    commands = ServerCommandRegister(app_ctx=engine)
    commands.register_module(JobCommandModule())
    commands.finalize()

    def send_request(*, request, **_kwargs):
        if not answers:
            return SimpleNamespace(payload=None)
        command = next(
            item["data"]
            for item in validate_proto(request.payload)["data"]
            if item["type"] == "command"
        )
        connection = Connection()
        commands.process_command(connection, command)
        return SimpleNamespace(payload=connection.close())

    api = AdminAPI.__new__(AdminAPI)  # without logging in
    api.in_logout = False
    api._debug = False
    api.user_name = "admin@nvidia.com"
    api.event_handlers = []
    api.token = None
    api.cmd_timeout = None
    api.cell = SimpleNamespace(send_request=send_request)
    return api


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
        self.session.api.do_command.return_value = flare_list_jobs_reply([])
        self.session.submit_job.side_effect = self._capture_submission

        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in (
            "SWARMMEDHUB_SWARM_ROUNDS",
            "SWARMMEDHUB_RESUME_CHECKPOINTS",
            *training_services.SWARM_TIME_LIMIT_ENV.values(),
            "SWARMMEDHUB_FLARE_ADMIN_TIMEOUT",
        ):
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

    def _submitted_rounds(self):
        """The start round and the number of rounds of the submitted job."""
        config = self.submitted[-1] / "app_server/config/config_fed_server.json"
        args = json.loads(config.read_text())["workflows"][0]["args"]
        return args.get("start_round", 0), args["num_rounds"]

    @contextlib.contextmanager
    def as_root(self):
        """Run as the documented CLI does, as root in the app container.

        Yields a recorder of ``os.chown``: what is handed to the worker's user
        (uid and gid 4242 here, appuser in a container).
        """
        with (
            patch.object(os, "geteuid", return_value=0),
            patch.object(os, "chown") as chown,
            patch.object(
                network_utils, "_runtime_service_identity", return_value=(4242, 4242)
            ),
        ):
            yield chown

    def assert_left_to_the_worker(self, chown, folder: Path, *, including: str):
        """Assert that the folder and everything in it was handed to the worker."""
        handed = {
            Path(call.args[0])
            for call in chown.call_args_list
            if call.args[1:] == (4242, 4242)
            and call.kwargs == {"follow_symlinks": False}
        }
        everything = {folder}
        for current, dirs, files in os.walk(folder):
            everything.update(Path(current, name) for name in (*dirs, *files))
        self.assertIn(folder / including, everything)
        self.assertEqual(
            sorted(str(path.relative_to(folder)) for path in everything - handed), []
        )


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

    def _plan_from_round(self, next_round=3):
        source = self.base_dir / "ckpt"
        _write_round_start_checkpoint(source, next_round, "node-B")
        return ResumePlan(
            checkpoint=ResumeCheckpoint(
                path=str(source / CHECKPOINT_FILE),
                next_round=next_round,
                site="node-B",
                source="round-start",
            ),
            start_round=next_round,
            total_rounds=10,
        )

    def _controller_args(self):
        server, _clients = self._configs(self.submitted[-1])
        return server["workflows"][0]["args"]

    def test_new_job_keeps_the_nvflare_start_timeout(self):
        training_services.submit_training_job(actor=self.user, network=self.network)

        args = self._controller_args()
        self.assertNotIn("start_task_timeout", args)
        self.assertNotIn("job_status_check_interval", args)

    def test_resumed_job_waits_longer_for_the_workflow_to_start(self):
        log = MagicMock()
        with patch.object(training_services, "get_logger", return_value=log):
            training_services.submit_training_job(
                actor=self.user, network=self.network, resume=self._plan_from_round()
            )

        args = self._controller_args()
        timeout = args["start_task_timeout"]
        self.assertEqual(timeout, training_services.RESUME_START_TASK_TIMEOUT)
        # The job's JSON keeps it an int, which NVFlare requires.
        self.assertIs(type(timeout), int)
        self.assertEqual(
            args["job_status_check_interval"],
            training_services.RESUME_JOB_STATUS_CHECK_INTERVAL,
        )
        config_line = next(
            call.args[0]
            for call in log.training.info.call_args_list
            if call.args[0].startswith("Swarm controller config")
        )
        self.assertIn("'start_task_timeout': 600", config_line)
        self.assertIn("'job_status_check_interval': 10.0", config_line)
        self.assertIn("default for a resumed job", config_line)

    def test_the_variable_sets_the_start_timeout_of_a_resumed_job(self):
        os.environ["SWARMMEDHUB_SWARM_START_TASK_TIMEOUT"] = "120.0"

        training_services.submit_training_job(
            actor=self.user, network=self.network, resume=self._plan_from_round()
        )

        timeout = self._controller_args()["start_task_timeout"]
        self.assertEqual((timeout, type(timeout)), (120, int))

    def _set_limits_equal_to_nvflare_defaults(self):
        # NVIDIA FLARE 2.7.1's defaults, which its job export leaves out; the FL
        # server's image can have other defaults (100 h in the worked example's).
        os.environ.update(
            {
                "SWARMMEDHUB_SWARM_START_TASK_TIMEOUT": "10",
                "SWARMMEDHUB_SWARM_MAX_STATUS_REPORT_INTERVAL": "90",
                "SWARMMEDHUB_SWARM_PROGRESS_TIMEOUT": "3600",
            }
        )
        return {
            "start_task_timeout": 10,
            "max_status_report_interval": 90.0,
            "progress_timeout": 3600.0,
        }

    def test_limits_equal_to_nvflare_defaults_reach_a_new_job(self):
        expected = self._set_limits_equal_to_nvflare_defaults()

        training_services.submit_training_job(actor=self.user, network=self.network)

        args = self._controller_args()
        self.assertEqual({name: args.get(name) for name in expected}, expected)
        self.assertIs(type(args["start_task_timeout"]), int)
        self.assertNotIn("job_status_check_interval", args)

    def test_limits_equal_to_nvflare_defaults_reach_a_resumed_job(self):
        expected = self._set_limits_equal_to_nvflare_defaults()

        training_services.submit_training_job(
            actor=self.user, network=self.network, resume=self._plan_from_round()
        )

        args = self._controller_args()
        self.assertEqual({name: args.get(name) for name in expected}, expected)
        self.assertIs(type(args["start_task_timeout"]), int)
        self.assertEqual(args["job_status_check_interval"], 10.0)
        self.assertEqual((args["start_round"], args["num_rounds"]), (3, 7))

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
            total_rounds=4,
        )
        with self.assertRaisesRegex(ValueError, "Nothing to resume"):
            training_services.submit_training_job(
                actor=self.user, network=self.network, resume=plan
            )
        self.assertEqual(TrainingJob.objects.count(), 0)

    def _run_files(self, job: Path) -> dict:
        """The run file of each app of a submitted job, None where it is missing."""
        return {
            app.name: json.loads(path.read_text()) if path.is_file() else None
            for app in sorted(job.glob("app_*"))
            for path in [app / "config" / "swarmmed_run.json"]
        }

    def test_every_app_describes_the_run(self):
        os.environ["SWARMMEDHUB_SWARM_ROUNDS"] = "8"

        training_services.submit_training_job(actor=self.user, network=self.network)

        expected = {
            "format": 1,
            "network": str(self.network.identifier),
            "total_rounds": 8,
            "start_round": 0,
            "num_rounds": 8,
            "from_flare_job": "",
        }
        self.assertEqual(
            self._run_files(self.submitted[0]),
            {app: expected for app in ("app_node-A", "app_node-B", "app_server")},
        )

    def test_a_resumed_job_describes_the_whole_run(self):
        old_uuid = str(uuid.uuid4())
        plan = self._plan_from_round(3)
        plan.total_rounds = 8
        plan.from_flare_job = old_uuid

        training_services.submit_training_job(
            actor=self.user, network=self.network, resume=plan
        )

        expected = {
            "format": 1,
            "network": str(self.network.identifier),
            "total_rounds": 8,
            "start_round": 3,
            "num_rounds": 5,
            "from_flare_job": old_uuid,
        }
        self.assertEqual(
            self._run_files(self.submitted[0]),
            {app: expected for app in ("app_node-A", "app_node-B", "app_server")},
        )

    def test_a_resume_without_a_total_is_not_submitted(self):
        plan = self._plan_from_round(3)
        plan.total_rounds = None

        with self.assertRaisesRegex(
            ValueError, "needs the run's total number of rounds"
        ):
            training_services.submit_training_job(
                actor=self.user, network=self.network, resume=plan
            )
        self.assertEqual(self.submitted, [])
        self.assertEqual(TrainingJob.objects.count(), 0)

    def test_a_job_submitted_as_root_leaves_its_folder_to_the_worker(self):
        # The web interface and the worker, which run as appuser, write the
        # network's next job into the same folder.
        with self.as_root() as chown:
            training_services.submit_training_job(
                actor=self.user, network=self.network
            )

        self.assert_left_to_the_worker(
            chown, self.network_dir / "job", including="generated"
        )

    def test_a_failed_submission_still_leaves_the_folder_to_the_worker(self):
        self.session.submit_job.side_effect = RuntimeError("submission refused")

        with (
            self.as_root() as chown,
            self.assertRaisesMessage(RuntimeError, "submission refused"),
        ):
            training_services.submit_training_job(
                actor=self.user, network=self.network
            )

        self.assert_left_to_the_worker(
            chown, self.network_dir / "job", including="app_client/custom"
        )


class ServerRouting:
    """Routes the admin sessions of a resume: the current server is the harness's
    fake session, which lists no job; a session for one address alone
    (``only_host``) goes to the previous server.

    The previous server cannot be reached unless ``self.previous_server_state``
    sets the state in which it lists the interrupted job, or
    ``self.previous_server_reply`` its reply to ``list_jobs``.
    ``self.session_calls`` collects the ``only_host`` of every session, "" for
    the current server, and ``self.session_timeouts`` their timeouts.
    """

    def setUp(self):
        super().setUp()
        os.environ.pop("SWARMMEDHUB_SERVER_HOST", None)
        os.environ.pop(training_services.FLARE_CONNECT_TIMEOUT_ENV, None)
        self.session_calls: list[str] = []
        self.session_timeouts: list[float] = []
        self.previous_server_state = None
        self.previous_server_reply = None
        training_services.new_secure_session_with_host.side_effect = self._session_for

    def _session_for(self, *_args, **kwargs):
        only_host = kwargs.get("only_host", "")
        self.session_calls.append(only_host)
        self.session_timeouts.append(kwargs.get("timeout"))
        if not only_host:
            return self.session
        if self.previous_server_state is None and self.previous_server_reply is None:
            raise RuntimeError(
                f"cannot connect to FLARE admin API. Attempts: host={only_host} "
                "port=8003: connection refused"
            )
        previous = MagicMock()
        previous.api.do_command.return_value = self.previous_server_reply or (
            flare_list_jobs_reply(
                [{"job_id": self.old_uuid, "status": self.previous_server_state}]
            )
        )
        return previous

    def switch_servers(self, **fields):
        """Write a switch record as 'swarmed network failover' does at a site."""
        prod_dir = self.client_workspace("node-A").parent
        prod_dir.mkdir(parents=True, exist_ok=True)
        (prod_dir / ".active_server.json").write_text(
            json.dumps(
                {
                    "server_host": "192.0.2.10",
                    "previous_server_host": "100.64.0.1",
                    "provisioned_server_host": "100.64.0.1",
                    "server_started_here": False,
                    **fields,
                }
            )
        )


class ResumeTrainingJobTests(ServerRouting, SubmitHarness, TestCase):
    def setUp(self):
        self.old_uuid = str(uuid.uuid4())
        super().setUp()
        self.old_job = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id=self.old_uuid,
            flare_job_uuid=self.old_uuid,
            total_rounds=8,
        )

    def _checkpoint_at(self, client: str, next_round: int, **fields):
        app_dir = self.client_workspace(client) / self.old_uuid / f"app_{client}"
        return _write_round_start_checkpoint(app_dir, next_round, client, **fields)

    def _job_file(self, participant: str, relative: str, content, *, job=None):
        """Write a file into a job's folder here, the interrupted job's by default."""
        path = self.client_workspace(participant) / (job or self.old_uuid) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content))
        return path

    def _forget_the_total(self):
        """As a record that a site's job monitor created, or that has no total."""
        TrainingJob.objects.filter(pk=self.old_job.pk).update(total_rounds=None)
        self.old_job.refresh_from_db()

    def _assert_not_resumed(self):
        self.assertEqual(self.submitted, [])
        self.old_job.refresh_from_db()
        self.assertEqual(self.old_job.status, "RUNNING")

    def test_resumes_from_the_newest_checkpoint_and_fails_the_old_job(self):
        self._checkpoint_at("node-A", 2)
        self._checkpoint_at("node-B", 3)

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

    def test_a_resume_run_as_root_leaves_the_job_folder_to_the_worker(self):
        self._checkpoint_at("node-A", 3)

        with self.as_root() as chown:
            training_services.resume_training_job(
                actor=self.user, network=self.network
            )

        self.assert_left_to_the_worker(
            chown, self.network_dir / "job", including="resumed_jobs.json"
        )

    def test_an_interrupted_job_that_has_ended_keeps_its_status(self):
        self._checkpoint_at("node-A", 2)
        # 'swarmed network stop' marks a running job as stopped, a switch as failed.
        for status in ("STOPPED", "FAILED"):
            with self.subTest(status=status):
                TrainingJob.objects.filter(pk=self.old_job.pk).update(status=status)
                self.old_job.refresh_from_db()

                training_services.resume_training_job(
                    actor=self.user, network=self.network, from_job=self.old_job
                )

                self.old_job.refresh_from_db()
                self.assertEqual(self.old_job.status, status)

    def test_refuses_while_the_server_still_runs_the_job(self):
        self._checkpoint_at("node-A", 2)
        self.session.api.do_command.return_value = flare_list_jobs_reply(
            [{"job_id": self.old_uuid, "status": "RUNNING"}]
        )

        with self.assertRaisesRegex(
            ValueError, "still active on the network's FL server"
        ) as refused:
            training_services.resume_training_job(actor=self.user, network=self.network)
        self._assert_not_resumed()
        # Some FLARE builds keep listing the job as running after a restart.
        self.assertIn(
            "If that server was restarted after the job was interrupted, its FLARE "
            "build may still list the job as running; then pass --force.",
            str(refused.exception),
        )

    def test_refuses_while_the_server_runs_another_job(self):
        # The run, already resumed at another site.
        self._checkpoint_at("node-A", 2)
        resumed_elsewhere = str(uuid.uuid4())
        self.session.api.do_command.side_effect = flare_admin_api(
            {self.old_uuid: "FINISHED:ABANDONED", resumed_elsewhere: "RUNNING"}
        ).server_execute

        with self.assertRaisesRegex(
            ValueError,
            rf"The network's FL server already runs job {resumed_elsewhere} "
            r"\(RUNNING\)\. If that job is this run, resumed at another site, do "
            r"not resume it again\..*--force",
        ):
            training_services.resume_training_job(actor=self.user, network=self.network)
        self._assert_not_resumed()

    def test_a_finished_job_on_the_server_does_not_block_a_resume(self):
        self._checkpoint_at("node-A", 2)
        self.session.api.do_command.side_effect = flare_admin_api(
            {
                self.old_uuid: "FINISHED:ABANDONED",
                str(uuid.uuid4()): "FINISHED:COMPLETED",
            }
        ).server_execute

        training_services.resume_training_job(actor=self.user, network=self.network)

        self.assertEqual(len(self.submitted), 1)

    def test_refuses_when_the_server_gives_no_job_list(self):
        self._checkpoint_at("node-A", 2)
        # What FLARE's admin API returns when list_jobs gets no answer in time.
        self.session.api.do_command.return_value = flare_admin_api(
            {self.old_uuid: "RUNNING"}, answers=False
        ).server_execute(f"list_jobs {self.old_uuid}")

        with self.assertRaisesRegex(
            ValueError,
            r"Could not ask the network's FL server .*Server did not respond.*--force",
        ):
            training_services.resume_training_job(actor=self.user, network=self.network)
        self._assert_not_resumed()

    def test_refuses_when_the_server_cannot_be_asked(self):
        self._checkpoint_at("node-A", 2)
        training_services.new_secure_session_with_host.side_effect = RuntimeError(
            "cannot connect to FLARE admin API"
        )

        with self.assertRaisesRegex(
            ValueError, r"Could not ask the network's FL server .* --force"
        ):
            training_services.resume_training_job(actor=self.user, network=self.network)
        self._assert_not_resumed()

    def test_refuses_when_this_hub_has_no_admin_kit(self):
        self._checkpoint_at("node-A", 2)

        with (
            patch.object(
                training_services, "resolve_admin_session_target", return_value=None
            ),
            self.assertRaisesRegex(ValueError, "no admin startup kit"),
        ):
            training_services.resume_training_job(actor=self.user, network=self.network)
        self._assert_not_resumed()

    def test_the_server_gets_as_long_to_answer_as_a_submission(self):
        self._checkpoint_at("node-A", 2)

        training_services.resume_training_job(actor=self.user, network=self.network)
        os.environ[training_services.FLARE_CONNECT_TIMEOUT_ENV] = "45"
        training_services.resume_training_job(
            actor=self.user, network=self.network, from_job=self.old_job
        )

        # Each resume: the check, then the submission.
        self.assertEqual(self.session_timeouts, [20.0, 20.0, 45.0, 45.0])
        # The check's list_jobs gets as long again, not the admin API's 5 s.
        self.assertEqual(
            [call.args for call in self.session.api.set_command_timeout.call_args_list],
            [(20.0,), (45.0,)],
        )

    def test_force_submits_without_asking_any_server(self):
        self._checkpoint_at("node-A", 2)
        self.switch_servers()
        self.previous_server_state = "RUNNING"

        training_services.resume_training_job(
            actor=self.user, network=self.network, force=True
        )

        self.assertEqual(len(self.submitted), 1)
        # Only the submission's session.
        self.assertEqual(self.session_calls, [""])

    def test_without_a_switch_only_the_current_server_is_asked(self):
        self._checkpoint_at("node-A", 2)
        self.previous_server_state = "RUNNING"

        training_services.resume_training_job(actor=self.user, network=self.network)

        self.assertEqual(self.session_calls, ["", ""])
        self.assertEqual(len(self.submitted), 1)

    def test_after_a_switch_refuses_while_the_previous_server_runs_the_job(self):
        self._checkpoint_at("node-A", 2)
        self.switch_servers()
        self.previous_server_state = "RUNNING"

        with self.assertRaisesRegex(
            ValueError, r"still active on the FL server at 100\.64\.0\.1.*--force"
        ):
            training_services.resume_training_job(actor=self.user, network=self.network)
        # The standby has an empty job store; only the previous server knows.
        self.assertEqual(self.session_calls, ["", "100.64.0.1"])
        self._assert_not_resumed()

    def test_the_standby_also_asks_the_previous_server(self):
        self._checkpoint_at("node-A", 2)
        self.switch_servers(server_started_here=True)
        self.previous_server_state = "RUNNING"

        with self.assertRaisesRegex(ValueError, r"100\.64\.0\.1"):
            training_services.resume_training_job(actor=self.user, network=self.network)
        self._assert_not_resumed()

    def test_after_a_switch_an_unreachable_previous_server_gives_a_warning(self):
        self._checkpoint_at("node-A", 2)
        self.switch_servers()
        warnings = []

        new_job = training_services.resume_training_job(
            actor=self.user, network=self.network, warnings=warnings
        )

        self.assertEqual(new_job.status, "RUNNING")
        self.assertEqual(len(warnings), 1)
        self.assertIn("FL server at 100.64.0.1", warnings[0])
        self.assertIn("Keep that server down", warnings[0])

    def test_after_a_switch_a_previous_server_without_job_list_gives_a_warning(self):
        self._checkpoint_at("node-A", 2)
        self.switch_servers()
        self.previous_server_reply = flare_admin_api(
            {self.old_uuid: "RUNNING"}, answers=False
        ).server_execute(f"list_jobs {self.old_uuid}")
        warnings = []

        new_job = training_services.resume_training_job(
            actor=self.user, network=self.network, warnings=warnings
        )

        self.assertEqual(new_job.status, "RUNNING")
        self.assertEqual(self.session_calls, ["", "100.64.0.1", ""])
        self.assertEqual(len(warnings), 1)
        self.assertIn("FL server at 100.64.0.1", warnings[0])
        self.assertIn("Server did not respond", warnings[0])

    def test_after_a_switch_no_warning_once_the_previous_server_ended_the_job(self):
        self._checkpoint_at("node-A", 2)
        self.switch_servers()
        self.previous_server_state = "FINISHED:ABANDONED"
        warnings = []

        training_services.resume_training_job(
            actor=self.user, network=self.network, warnings=warnings
        )

        self.assertEqual(self.session_calls, ["", "100.64.0.1", ""])
        self.assertEqual(warnings, [])
        self.assertEqual(len(self.submitted), 1)

    def test_a_record_without_the_previous_server_asks_the_address_used_before(self):
        self._checkpoint_at("node-A", 2)
        # As in a record that names only the server; the Hub used
        # SWARMMEDHUB_SERVER_HOST before the switch.
        prod_dir = self.client_workspace("node-A").parent
        (prod_dir / ".active_server.json").write_text(
            json.dumps({"server_host": "192.0.2.10"})
        )
        os.environ["SWARMMEDHUB_SERVER_HOST"] = "100.64.0.7"
        self.previous_server_state = "RUNNING"

        with self.assertRaisesRegex(ValueError, r"100\.64\.0\.7"):
            training_services.resume_training_job(actor=self.user, network=self.network)

    def test_a_network_that_is_not_running_is_refused_before_asking(self):
        self._checkpoint_at("node-A", 2)
        SwarmNetwork.objects.filter(pk=self.network.pk).update(status="STOPPED")
        self.network.refresh_from_db()

        with self.assertRaisesRegex(ValueError, "is not running"):
            training_services.resume_training_job(actor=self.user, network=self.network)
        self.assertEqual(self.session_calls, [])

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

    def test_a_guessed_total_is_never_used(self):
        # The record's total as a site's job monitor stored it before 1.0.6.
        TrainingJob.objects.filter(pk=self.old_job.pk).update(total_rounds=10)
        self._checkpoint_at("node-A", 3, total_rounds=None)

        with self.assertRaisesRegex(ValueError, r"not known at this Hub.*--rounds"):
            training_services.resume_training_job(actor=self.user, network=self.network)
        self._assert_not_resumed()

    def test_the_resuming_hubs_round_count_is_not_used(self):
        # The harness's training script says SWARM_ROUNDS = 4.
        self._forget_the_total()
        self._checkpoint_at("node-A", 3, total_rounds=None)

        with self.assertRaisesRegex(ValueError, r"not known at this Hub.*--rounds"):
            training_services.resume_training_job(actor=self.user, network=self.network)
        self._assert_not_resumed()
        self.assertEqual(self.session_calls, [])

    def test_the_total_comes_from_the_jobs_run_file(self):
        self._forget_the_total()
        self._checkpoint_at("node-A", 3, total_rounds=None)
        run_file = self._job_file(
            "node-A",
            "app_node-A/config/swarmmed_run.json",
            {
                "format": 1,
                "network": str(self.network.identifier),
                "total_rounds": 8,
                "start_round": 0,
                "num_rounds": 8,
                "from_flare_job": "",
            },
        )

        new_job = training_services.resume_training_job(
            actor=self.user, network=self.network
        )

        self.assertEqual(self._submitted_rounds(), (3, 5))
        self.assertEqual(new_job.total_rounds, 8)
        (record,) = json.loads(
            (self.network_dir / "job" / "resumed_jobs.json").read_text()
        )
        self.assertEqual(record["total_rounds"], 8)
        self.assertEqual(
            record["total_rounds_from"],
            str(run_file.relative_to(self.network_dir / "workspace")),
        )

    def test_the_total_comes_from_that_jobs_server_config_only(self):
        # FLARE keeps the server's folder of a running or interrupted job.
        self._forget_the_total()
        self._checkpoint_at("node-A", 3, total_rounds=None)
        for job, rounds in ((str(uuid.uuid4()), 30), (self.old_uuid, 8)):
            self._job_file(
                "server",
                "app_server/config/config_fed_server.json",
                _swarm_server_config(num_rounds=rounds),
                job=job,
            )

        training_services.resume_training_job(actor=self.user, network=self.network)

        self.assertEqual(self._submitted_rounds(), (3, 5))

    def test_a_1_0_5_resume_record_gives_the_total(self):
        # The interrupted job resumed another one; SwarmMed 1.0.5 recorded that
        # in the starting site's app.
        self._forget_the_total()
        self._checkpoint_at("node-A", 10, total_rounds=None)
        self._job_file(
            "node-A",
            "app_node-A/config/swarmmed_resume.json",
            {
                "start_round": 3,
                "total_rounds": 40,
                "from_job": "",
                "from_flare_job": str(uuid.uuid4()),
                "checkpoint_source": "round-start",
                "checkpoint_site": "node-B",
                "checkpoint_round": 3,
                "num_tensors": 2,
            },
        )

        training_services.resume_training_job(actor=self.user, network=self.network)

        self.assertEqual(self._submitted_rounds(), (10, 30))

    def test_the_total_comes_from_a_copied_checkpoint(self):
        # No folder of the job here: the checkpoint was copied from another site.
        self._forget_the_total()
        copied = self.base_dir / "copied"
        _write_round_start_checkpoint(copied, 3, "node-B", job_id=self.old_uuid)

        training_services.resume_training_job(
            actor=self.user,
            network=self.network,
            checkpoint_path=str(copied / CHECKPOINT_FILE),
        )

        self.assertEqual(self._submitted_rounds(), (3, 5))

    def test_rounds_given_override_a_known_total_with_a_warning(self):
        self._checkpoint_at("node-A", 3, total_rounds=40)
        self._job_file(
            "node-A",
            "app_node-A/config/swarmmed_run.json",
            {"format": 1, "total_rounds": 40, "start_round": 0, "num_rounds": 40},
        )
        warnings = []

        training_services.resume_training_job(
            actor=self.user, network=self.network, total_rounds=30, warnings=warnings
        )

        self.assertEqual(self._submitted_rounds(), (3, 27))
        self.assertEqual(len(warnings), 1)
        self.assertIn("--rounds 30 differs from the run's 40 rounds", warnings[0])

    def test_a_total_beyond_any_run_in_the_jobs_files_is_not_used(self):
        # Code in the FL client containers can write the job's files.
        self._checkpoint_at("node-A", 3, total_rounds=None)
        self._job_file(
            "node-A",
            "app_node-A/config/swarmmed_run.json",
            {"format": 1, "total_rounds": 3_000_000_000, "start_round": 0},
        )

        with self.assertRaisesMessage(ValueError, "Pass --rounds"):
            training_services.resume_training_job(actor=self.user, network=self.network)
        self._assert_not_resumed()
        self.assertEqual(self.session_calls, [])

    def test_rounds_beyond_any_run_are_refused_before_any_server_is_asked(self):
        self._checkpoint_at("node-A", 3)

        for rounds in (0, 100_001, 3_000_000_000):
            with (
                self.subTest(rounds=rounds),
                self.assertRaisesMessage(
                    ValueError, "--rounds must be between 1 and 100000."
                ),
            ):
                training_services.resume_training_job(
                    actor=self.user, network=self.network, total_rounds=rounds
                )
        self._assert_not_resumed()
        self.assertEqual(self.session_calls, [])

    def test_active_check_reads_the_server_job_list(self):
        other_uuid = str(uuid.uuid4())
        for listed, expected in (
            ([{"job_id": self.old_uuid, "status": "RUNNING"}], True),
            ([{"job_id": self.old_uuid.upper(), "status": "DISPATCHED"}], True),
            ([{"job_id": self.old_uuid, "status": "FINISHED:ABANDONED"}], False),
            ([{"job_id": other_uuid, "status": "RUNNING"}], False),
            ([], False),
        ):
            with self.subTest(listed=listed):
                self.session.api.do_command.return_value = flare_list_jobs_reply(listed)
                self.assertEqual(
                    training_services._job_active_on_server(self.network, self.old_job),
                    (expected, ""),
                )
        self.session.api.do_command.assert_called_with(f"list_jobs {self.old_uuid}")

        # A refused command, as FLARE's admin API returns it.
        self.session.api.do_command.return_value = {
            "time": "2026-01-01 00:00:00.000000",
            "data": [{"type": "error", "data": "This action is not authorized"}],
            "meta": {"status": "not_authorized", "info": "not authorized"},
            "status": "ERROR_AUTHORIZATION",
        }
        self.assertEqual(
            training_services._job_active_on_server(self.network, self.old_job),
            (
                None,
                "list_jobs gave no job list: ERROR_AUTHORIZATION; not_authorized; "
                "not authorized",
            ),
        )
        # As FLARE's own Session, trust a job list only with the status SUCCESS.
        self.session.api.do_command.return_value = {
            **flare_list_jobs_reply([{"job_id": self.old_uuid, "status": "RUNNING"}]),
            "status": "ERROR_INACTIVE_SESSION",
        }
        self.assertEqual(
            training_services._job_active_on_server(self.network, self.old_job),
            (None, "list_jobs gave no job list: ERROR_INACTIVE_SESSION"),
        )
        self.session.api.do_command.side_effect = RuntimeError("session closed")
        active, reason = training_services._job_active_on_server(
            self.network, self.old_job
        )
        self.assertEqual((active, reason), (None, "list_jobs failed: session closed"))


class FlareListJobsReplyTests(SimpleTestCase):
    """The check before a resume, with replies made by NVIDIA FLARE 2.7.1's code."""

    def setUp(self):
        self.job_uuid = str(uuid.uuid4())
        self.job = SimpleNamespace(
            flare_job_uuid=self.job_uuid, flare_job_id=self.job_uuid
        )
        self.network = SimpleNamespace(identifier=uuid.uuid4())
        self.session = MagicMock()
        for target, value in {
            "resolve_admin_session_target": (
                "admin@nvidia.com",
                "/nonexistent",
                "192.0.2.10",
            ),
            "new_secure_session_with_host": self.session,
        }.items():
            patcher = patch.object(training_services, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _ask(self, api):
        self.session.api.do_command.side_effect = api.server_execute
        return training_services._job_active_on_server(
            self.network, self.job, host="100.64.0.1"
        )

    def test_a_job_that_the_server_runs_is_active(self):
        for state in ("SUBMITTED", "DISPATCHED", "RUNNING"):
            with self.subTest(state=state):
                api = flare_admin_api(
                    {self.job_uuid: state, str(uuid.uuid4()): "FINISHED:COMPLETED"}
                )
                self.assertEqual(self._ask(api), (True, ""))

    def test_a_finished_or_unknown_job_is_not_active(self):
        for jobs in (
            {self.job_uuid: "FINISHED:ABANDONED"},
            {str(uuid.uuid4()): "RUNNING"},
            {},
        ):
            with self.subTest(jobs=jobs):
                self.assertEqual(self._ask(flare_admin_api(jobs)), (False, ""))

    def test_a_server_gets_the_connect_timeout_to_answer(self):
        # FLARE's admin API waits 5 s for the answer to a command unless the
        # session sets another time. This server needs 8 s to answer.
        api = flare_admin_api({self.job_uuid: "RUNNING"})
        answer = api.cell.send_request
        timeouts = []

        def send_request(**kwargs):
            timeouts.append(kwargs["timeout"])
            if kwargs["timeout"] < 8.0:
                # FLARE's cell gives up before the answer arrives.
                return SimpleNamespace(payload=None)
            return answer(**kwargs)

        api.cell = SimpleNamespace(send_request=send_request)
        self.session.api.set_command_timeout.side_effect = api.set_command_timeout
        environment = {training_services.FLARE_CONNECT_TIMEOUT_ENV: "45"}

        with patch.dict(os.environ, environment):
            self.assertEqual(self._ask(api), (True, ""))
        self.assertEqual(timeouts, [45.0])

    def test_the_logout_keeps_the_admin_api_timeout(self):
        # A server that accepted the login but does not answer: the logout after
        # list_jobs waits FLARE's own 5 s, not the longer time list_jobs got.
        api = flare_admin_api({self.job_uuid: "RUNNING"}, answers=False)
        answer = api.cell.send_request
        timeouts = []

        def send_request(**kwargs):
            timeouts.append(kwargs["timeout"])
            return answer(**kwargs)

        api.cell = SimpleNamespace(send_request=send_request, stop=lambda: None)
        api.closed = False
        api.server_sess_active = True
        api.shutdown_streamer = lambda: None
        self.session.api.set_command_timeout.side_effect = api.set_command_timeout
        self.session.api.unset_command_timeout.side_effect = (
            api.unset_command_timeout
        )
        self.session.close.side_effect = api.logout  # as FLARE's Session.close
        environment = {training_services.FLARE_CONNECT_TIMEOUT_ENV: "45"}

        with patch.dict(os.environ, environment):
            self._ask(api)
        # list_jobs, then the logout.
        self.assertEqual(timeouts, [45.0, 5.0])

    def test_a_server_that_does_not_answer_in_time_cannot_be_asked(self):
        api = flare_admin_api({self.job_uuid: "RUNNING"}, answers=False)

        self.assertEqual(
            self._ask(api),
            (
                None,
                "list_jobs gave no job list: ERROR_SERVER_CONNECTION; "
                "Server did not respond",
            ),
        )

    def test_a_server_that_cannot_read_its_job_store_cannot_be_asked(self):
        # FLARE's reply has the status SUCCESS here; only its meta has the error.
        api = flare_admin_api(
            {self.job_uuid: "RUNNING"}, store_error=RuntimeError("job store gone")
        )

        self.assertEqual(
            self._ask(api),
            (
                None,
                "list_jobs gave no job list: internal_error; "
                "RuntimeError: job store gone",
            ),
        )

    def test_a_server_that_fails_to_list_the_job_cannot_be_asked(self):
        # FLARE cannot read this running job's start time. Its reply keeps the
        # rows listed before the error, none here, in meta["jobs"].
        api = flare_admin_api(
            {self.job_uuid: {"status": "RUNNING", "start_time": "unknown"}}
        )

        active, reason = self._ask(api)

        self.assertIsNone(active)
        self.assertIn("list_jobs gave no job list: internal_error; ValueError", reason)

    def test_the_stand_in_reply_has_the_shape_of_flares(self):
        def essentials(reply):
            return (
                reply["status"],
                [item["type"] for item in reply["data"]],
                {key: value for key, value in reply["meta"].items() if key != "jobs"},
                [(job["job_id"], job["status"]) for job in reply["meta"]["jobs"]],
            )

        for jobs in ({self.job_uuid: "RUNNING"}, {}):
            with self.subTest(jobs=jobs):
                flare = flare_admin_api(jobs).server_execute(
                    f"list_jobs {self.job_uuid}"
                )
                stand_in = flare_list_jobs_reply(
                    [{"job_id": job, "status": state} for job, state in jobs.items()]
                )
                self.assertEqual(essentials(stand_in), essentials(flare))


class ResumeCommandTests(ServerRouting, SubmitHarness, TestCase):
    """`swarmed training resume`, with the FL server replaced by a fake session."""

    def setUp(self):
        self.old_uuid = str(uuid.uuid4())
        super().setUp()
        TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id=self.old_uuid,
            flare_job_uuid=self.old_uuid,
            total_rounds=8,
        )
        app_dir = self.client_workspace("node-A") / self.old_uuid / "app_node-A"
        _write_round_start_checkpoint(app_dir, 3, "node-A")

    def _resume(self):
        stdout = StringIO()
        with (
            # Keep the checkout's .env, which the CLI loads, out of the test.
            patch("swarmed_cli.bootstrap.load_dotenv"),
            redirect_stdout(stdout),
        ):
            code = main(
                ["training", "resume", self.old_uuid, "--user", "resumer", "--json"]
            )
        envelope = json.loads(stdout.getvalue())
        self.assertEqual(code, 0, envelope)
        self.assertEqual(len(self.submitted), 1)
        return envelope

    def test_points_to_the_timeouts_that_a_slow_link_needs(self):
        warnings = self._resume()["warnings"]

        self.assertEqual(len(warnings), 1)
        self.assertIn("ships its checkpoint", warnings[0])
        self.assertIn("SWARMMEDHUB_FLARE_ADMIN_TIMEOUT", warnings[0])
        self.assertIn("SWARMMEDHUB_SWARM_START_TASK_TIMEOUT", warnings[0])

    def test_says_nothing_once_the_admin_timeout_is_set(self):
        os.environ["SWARMMEDHUB_FLARE_ADMIN_TIMEOUT"] = "300"

        self.assertEqual(self._resume()["warnings"], [])

    def test_names_a_previous_server_that_could_not_be_asked(self):
        os.environ["SWARMMEDHUB_FLARE_ADMIN_TIMEOUT"] = "300"
        self.switch_servers()

        warnings = self._resume()["warnings"]

        self.assertEqual(len(warnings), 1)
        self.assertIn("FL server at 100.64.0.1", warnings[0])
        self.assertIn("Keep that server down", warnings[0])


class ResumeWithoutRecordTests(ServerRouting, SubmitHarness, TestCase):
    """`swarmed training checkpoints` and `resume` at a Hub with no record of the job.

    As at the standby after a server switch: its job monitor never recorded the
    interrupted job, but the site's folder of the job holds its checkpoints.
    """

    def setUp(self):
        self.old_uuid = str(uuid.uuid4())
        super().setUp()
        # Leave out the note on slow links that every resume prints.
        os.environ["SWARMMEDHUB_FLARE_ADMIN_TIMEOUT"] = "300"
        self.network_id = str(self.network.identifier)

    def _checkpoint_at(self, client: str, next_round: int, *, job=None, **fields):
        app_dir = self.client_workspace(client) / (job or self.old_uuid)
        app_dir = app_dir / f"app_{client}"
        _write_round_start_checkpoint(app_dir, next_round, client, **fields)
        return app_dir / CHECKPOINT_FILE

    def _record(self, flare_uuid: str) -> TrainingJob:
        return TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id=flare_uuid,
            flare_job_uuid=flare_uuid,
            total_rounds=8,
        )

    def _cli(self, *arguments):
        stdout = StringIO()
        with (
            # Keep the checkout's .env, which the CLI loads, out of the test.
            patch("swarmed_cli.bootstrap.load_dotenv"),
            redirect_stdout(stdout),
        ):
            code = main(["training", *arguments, "--user", "resumer", "--json"])
        return code, json.loads(stdout.getvalue())

    def _resume_records(self):
        return json.loads((self.network_dir / "job" / "resumed_jobs.json").read_text())

    def test_checkpoints_are_listed_without_a_record(self):
        self._checkpoint_at("node-A", 3)

        code, envelope = self._cli(
            "checkpoints", self.old_uuid, "--network", self.network_id
        )

        self.assertEqual(code, 0, envelope)
        result = envelope["result"]
        self.assertEqual(
            (result["job_identifier"], result["flare_job_id"], result["found_by"]),
            (None, self.old_uuid, "job folder"),
        )
        self.assertEqual(
            [(c["site"], c["next_round"], c["job_id"]) for c in result["checkpoints"]],
            [("node-A", 3, self.old_uuid)],
        )
        self.assertEqual(result["total_rounds"], 8)
        self.assertEqual(TrainingJob.objects.count(), 0)

    def test_checkpoints_find_the_network_from_the_job_folder(self):
        # Another network of the user's, without a folder of the job.
        SwarmNetwork.objects.create(
            name="Second Network",
            project=self.project,
            author=self.user,
            status="RUNNING",
        )
        self._checkpoint_at("node-A", 3)

        code, envelope = self._cli("checkpoints", self.old_uuid)

        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["result"]["network_identifier"], self.network_id)
        self.assertEqual(len(envelope["result"]["checkpoints"]), 1)

    def test_checkpoints_take_the_start_of_a_flare_job_id(self):
        self._checkpoint_at("node-A", 3)

        code, envelope = self._cli(
            "checkpoints", self.old_uuid[:8], "--network", self.network_id
        )

        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["result"]["flare_job_id"], self.old_uuid)

        # Another job whose ID starts the same way.
        self._checkpoint_at("node-A", 2, job=self.old_uuid[:9] + str(uuid.uuid4())[9:])
        code, envelope = self._cli(
            "checkpoints", self.old_uuid[:8], "--network", self.network_id
        )

        self.assertEqual(code, 2, envelope)
        self.assertIn(f"'{self.old_uuid[:8]}' matches 2 jobs", envelope["errors"][0])
        self.assertIn("give more of the FLARE job ID", envelope["errors"][0])

    def test_resume_without_a_record_records_both_jobs(self):
        self._checkpoint_at("node-A", 3)

        code, envelope = self._cli(
            "resume", self.old_uuid, "--network", self.network_id
        )

        self.assertEqual(code, 0, envelope)
        self.assertEqual(len(self.submitted), 1)
        self.assertEqual(self._submitted_rounds(), (3, 5))
        new_job = TrainingJob.objects.get(identifier=envelope["result"]["identifier"])
        self.assertEqual((new_job.status, new_job.total_rounds), ("RUNNING", 8))
        old_job = TrainingJob.objects.get(
            network=self.network, flare_job_uuid=self.old_uuid
        )
        self.assertEqual((old_job.status, old_job.total_rounds), ("FAILED", 8))
        self.assertIsNotNone(old_job.completed_at)
        (record,) = self._resume_records()
        self.assertEqual(
            (record["from_job"], record["from_flare_job"]),
            (str(old_job.identifier), self.old_uuid),
        )
        self.assertEqual(len(envelope["warnings"]), 1)
        self.assertIn(f"had no record of job {self.old_uuid}", envelope["warnings"][0])

    def test_network_and_checkpoint_alone_take_the_job_from_the_checkpoint(self):
        checkpoint = self._checkpoint_at("node-A", 3)

        code, envelope = self._cli(
            "resume",
            "--network",
            self.network_id,
            "--checkpoint",
            str(checkpoint),
            "--start-round",
            "3",
            "--rounds",
            "8",
        )

        self.assertEqual(code, 0, envelope)
        (record,) = self._resume_records()
        self.assertEqual(record["from_flare_job"], self.old_uuid)
        self.assertEqual(self._submitted_rounds(), (3, 5))

    def test_a_copied_checkpoint_needs_no_json_and_no_start_round(self):
        # Saved at another site by a job of SwarmMed 1.0.5; only the .pt was copied.
        source = self.base_dir / "site-b" / self.old_uuid / "app_node-B"
        _write_round_start_checkpoint(source, 3, "node-B", total_rounds=None)
        copied = self.base_dir / "copied" / "copy.pt"
        copied.parent.mkdir()
        shutil.copyfile(source / CHECKPOINT_FILE, copied)

        code, envelope = self._cli(
            "resume",
            "--network",
            self.network_id,
            "--checkpoint",
            str(copied),
            "--rounds",
            "8",
        )

        self.assertEqual(code, 0, envelope)
        self.assertEqual(self._submitted_rounds(), (3, 5))
        (record,) = self._resume_records()
        self.assertEqual(
            (record["from_flare_job"], record["checkpoint"]["site"]),
            (self.old_uuid, "node-B"),
        )

    def test_a_copied_checkpoint_is_resumed_in_its_jobs_network(self):
        # A checkpoint of SwarmMed 1.0.5 names its job but no network. Copied
        # out of the job's folder, it is given while another network is current.
        copied = self.base_dir / "copied" / "copy.pt"
        copied.parent.mkdir()
        shutil.copyfile(self._checkpoint_at("node-A", 3, total_rounds=None), copied)
        current = SwarmNetwork.objects.create(
            name="Current Network",
            project=self.project,
            author=self.user,
            status="RUNNING",
        )
        current_dir = (
            self.base_dir
            / "workspaces"
            / str(self.project.identifier)
            / str(current.identifier)
        )
        current_dir.mkdir(parents=True)
        (current_dir / "runtime_requirements.txt").write_text("torch\n")
        set_current_network(self.user, current)

        code, envelope = self._cli(
            "resume", "--checkpoint", str(copied), "--rounds", "8"
        )

        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["result"]["network_identifier"], self.network_id)
        self.assertFalse(TrainingJob.objects.filter(network=current).exists())

    def test_a_model_file_that_names_no_job_is_resumed_in_the_current_network(self):
        self._checkpoint_at("node-A", 3)
        model = self.base_dir / "copied" / "model.pt"
        model.parent.mkdir()
        torch.save({"model": {"fc.weight": torch.ones(2, 4)}}, model)
        arguments = (
            "resume",
            "--checkpoint",
            str(model),
            "--start-round",
            "3",
            "--rounds",
            "8",
        )

        code, envelope = self._cli(*arguments)

        self.assertEqual(code, 2, envelope)
        self.assertIn("No current network is selected", envelope["errors"][0])

        set_current_network(self.user, self.network)
        code, envelope = self._cli(*arguments)

        self.assertEqual(code, 0, envelope)
        (record,) = self._resume_records()
        self.assertEqual(record["from_flare_job"], self.old_uuid)
        self.assertEqual(self._submitted_rounds(), (3, 5))

    def test_a_checkpoint_of_another_job_is_refused(self):
        self._record(self.old_uuid)
        other_job = str(uuid.uuid4())
        checkpoint = self._checkpoint_at("node-A", 3, job=other_job)

        code, envelope = self._cli(
            "resume",
            self.old_uuid,
            "--network",
            self.network_id,
            "--checkpoint",
            str(checkpoint),
        )

        self.assertEqual(code, 2, envelope)
        self.assertIn(other_job, envelope["errors"][0])
        self.assertIn(self.old_uuid, envelope["errors"][0])
        self.assertEqual(self.submitted, [])

    def test_the_checkpoints_folder_names_the_network(self):
        # The job and its checkpoint without --network, from a job of SwarmMed
        # 1.0.5; another network of the user's also holds a folder of the job.
        second = SwarmNetwork.objects.create(
            name="Second Network",
            project=self.project,
            author=self.user,
            status="RUNNING",
        )
        (
            self.base_dir
            / "workspaces"
            / str(self.project.identifier)
            / str(second.identifier)
            / "workspace"
            / "resume_project"
            / "prod_00"
            / "node-A"
            / self.old_uuid
        ).mkdir(parents=True)
        checkpoint = self._checkpoint_at("node-A", 3, total_rounds=None)

        code, envelope = self._cli(
            "resume",
            self.old_uuid,
            "--checkpoint",
            str(checkpoint),
            "--start-round",
            "3",
            "--rounds",
            "8",
        )

        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["result"]["network_identifier"], self.network_id)
        self.assertEqual(self._submitted_rounds(), (3, 5))

    def test_a_checkpoint_of_another_network_is_refused(self):
        self._record(self.old_uuid)
        other_network = str(uuid.uuid4())
        checkpoint = self._checkpoint_at("node-A", 3, network=other_network)

        code, envelope = self._cli(
            "resume",
            self.old_uuid,
            "--network",
            self.network_id,
            "--checkpoint",
            str(checkpoint),
        )

        self.assertEqual(code, 2, envelope)
        self.assertIn(f"belongs to network {other_network}", envelope["errors"][0])
        self.assertEqual(self.submitted, [])

    def test_without_a_record_the_standby_asks_both_servers(self):
        self._checkpoint_at("node-A", 3)
        self.switch_servers(server_started_here=True)

        code, envelope = self._cli(
            "resume", self.old_uuid, "--network", self.network_id
        )

        self.assertEqual(code, 0, envelope)
        # The current server, the previous one, then the submission.
        self.assertEqual(self.session_calls, ["", "100.64.0.1", ""])
        self.assertTrue(
            any("Keep that server down" in item for item in envelope["warnings"]),
            envelope["warnings"],
        )
        self.assertEqual(len(self.submitted), 1)

    def test_without_a_record_a_server_that_runs_the_job_refuses(self):
        self._checkpoint_at("node-A", 3)
        self.session.api.do_command.return_value = flare_list_jobs_reply(
            [{"job_id": self.old_uuid, "status": "RUNNING"}]
        )

        code, envelope = self._cli(
            "resume", self.old_uuid, "--network", self.network_id
        )

        self.assertEqual(code, 2, envelope)
        self.assertIn(
            f"Job {self.old_uuid} is still active on the network's FL server",
            envelope["errors"][0],
        )
        self.assertEqual(self.submitted, [])
        self.assertEqual(TrainingJob.objects.count(), 0)

    def test_without_a_job_the_newest_checkpoint_job_is_resumed(self):
        older, newer = str(uuid.uuid4()), str(uuid.uuid4())
        self._checkpoint_at("node-A", 3, job=newer, saved_at=200.0)
        # Written last here, but saved earlier by its job.
        self._checkpoint_at("node-A", 5, job=older, saved_at=100.0)

        code, envelope = self._cli(
            "resume", "--network", self.network_id, "--rounds", "8"
        )

        self.assertEqual(code, 0, envelope)
        (record,) = self._resume_records()
        self.assertEqual(record["from_flare_job"], newer)
        self.assertEqual(self._submitted_rounds(), (3, 5))

    def test_without_a_job_a_latest_record_of_another_job_is_refused(self):
        latest = str(uuid.uuid4())
        self._record(latest)
        self._checkpoint_at("node-A", 3)

        code, envelope = self._cli("resume", "--network", self.network_id)

        self.assertEqual(code, 2, envelope)
        self.assertIn(latest, envelope["errors"][0])
        self.assertIn(self.old_uuid, envelope["errors"][0])
        self.assertEqual(self.submitted, [])

    def test_without_a_job_a_failed_submission_does_not_hide_the_latest_job(self):
        # The previous run, with a round-start checkpoint here.
        previous = str(uuid.uuid4())
        TrainingJob.objects.filter(pk=self._record(previous).pk).update(
            status="COMPLETED"
        )
        self._checkpoint_at("node-A", 7, job=previous)
        # The interrupted job, which has no round-start checkpoint here.
        self._record(self.old_uuid)

        # A submission that failed records a job without a FLARE job ID.
        for failed in ("error", "unknown"):
            with self.subTest(failed=failed):
                row = TrainingJob.objects.create(
                    project=self.project,
                    network=self.network,
                    status="FAILED",
                    flare_job_id=failed,
                )

                code, envelope = self._cli("resume", "--network", self.network_id)

                self.assertEqual(code, 2, envelope)
                self.assertIn(self.old_uuid, envelope["errors"][0])
                self.assertIn(previous, envelope["errors"][0])
                row.delete()
        self.assertEqual(self.submitted, [])

    def test_an_identifier_that_is_no_job_id_is_not_looked_up_on_disk(self):
        with patch.object(
            training_services,
            "_network_workspace_root",
            side_effect=AssertionError("a network's workspace was looked at"),
        ):
            code, envelope = self._cli(
                "resume", "../../x", "--network", self.network_id
            )

        self.assertEqual(code, 2, envelope)
        self.assertEqual(envelope["errors"], ["Training job '../../x' was not found."])

    def test_networks_of_other_projects_are_not_searched(self):
        stranger = User.objects.create_user(
            username="stranger", password="test-password"
        )  # nosec B106
        project = Project.objects.create(title="Other Project", author=stranger)
        network = SwarmNetwork.objects.create(
            name="Other Network", project=project, author=stranger, status="RUNNING"
        )
        _write_round_start_checkpoint(
            self.base_dir
            / "workspaces"
            / str(project.identifier)
            / str(network.identifier)
            / "workspace"
            / "other_project"
            / "prod_00"
            / "node-A"
            / self.old_uuid
            / "app_node-A",
            3,
            "node-A",
        )

        for command in ("checkpoints", "resume"):
            with self.subTest(command=command):
                code, envelope = self._cli(command, self.old_uuid)

                self.assertEqual(code, 2, envelope)
                self.assertEqual(
                    envelope["errors"],
                    [f"Training job '{self.old_uuid}' was not found."],
                )
        self.assertEqual(self.submitted, [])

    def test_a_record_is_still_used_first(self):
        record = self._record(self.old_uuid)
        self._checkpoint_at("node-A", 3)

        code, envelope = self._cli(
            "checkpoints", str(record.identifier), "--network", self.network_id
        )

        self.assertEqual(code, 0, envelope)
        result = envelope["result"]
        self.assertEqual(
            (result["job_identifier"], result["flare_job_id"]),
            (str(record.identifier), self.old_uuid),
        )
        # The key is new in SwarmMed 1.0.6.
        self.assertEqual(result.get("found_by", "record"), "record")

        code, envelope = self._cli(
            "resume", self.old_uuid, "--network", self.network_id
        )

        self.assertEqual(code, 0, envelope)
        (resumed,) = self._resume_records()
        self.assertEqual(resumed["from_job"], str(record.identifier))
        self.assertEqual(TrainingJob.objects.count(), 2)
        self.assertEqual(envelope["warnings"], [])
