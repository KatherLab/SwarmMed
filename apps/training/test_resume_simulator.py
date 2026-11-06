"""Run a SwarmMed job and a resumed job in NVIDIA FLARE's simulator.

Slow (about a minute on a CPU) and skipped unless
``SWARMMEDHUB_RUN_FLARE_SIMULATOR=1``. It checks what the unit tests cannot:
that FLARE runs the job SwarmMed builds, that every site writes a round-start
checkpoint, and that a resumed job starts at the requested round from the
shipped checkpoint, with the same weights the interrupted run had at that round.

The simulator binds only free ports on localhost. By default it runs with the
FLARE version installed next to the Hub. ``SWARMMEDHUB_SIMULATOR_IMAGE`` runs it
inside a runtime image instead (for example the image a study trained with),
in a container without network access. ``SWARMMEDHUB_SIMULATOR_OUTPUT_DIR``
keeps the simulator output there.
"""

from __future__ import annotations

import os
import re
import subprocess  # nosec B404
import sys
from pathlib import Path
from unittest import skipUnless

import torch
from django.test import TestCase

from . import services as training_services
from .resume import CHECKPOINT_FILE, ResumePlan, find_resume_checkpoints
from .test_resume import SubmitHarness

TEST_LINE_RE = re.compile(
    r"SWARMMED_TEST site=(?P<site>\S+) round=(?P<round>\d+) "
    r"loaded=(?P<loaded>\w+) checksum=(?P<checksum>-?[\d.]+)"
)


def _run_simulator(job_dir: Path, workspace: Path, clients) -> str:
    environment = dict(os.environ, CUDA_VISIBLE_DEVICES="")
    simulator_args = ["-c", ",".join(clients), "-t", str(len(clients))]
    image = os.getenv("SWARMMEDHUB_SIMULATOR_IMAGE", "").strip()
    if image:
        # The simulator deletes and recreates its workspace, so mount the parent.
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "HOME=/tmp",
            "-e",
            "CUDA_VISIBLE_DEVICES=",
            "-v",
            f"{job_dir}:/sim/job:ro",
            "-v",
            f"{workspace.parent}:/sim/out",
            "--entrypoint",
            "python3",
            image,
            "-m",
            "nvflare.private.fed.app.simulator.simulator",
            "/sim/job",
            "-w",
            f"/sim/out/{workspace.name}",
            *simulator_args,
        ]
    else:
        command = [
            sys.executable,
            "-m",
            "nvflare.private.fed.app.simulator.simulator",
            str(job_dir),
            "-w",
            str(workspace),
            *simulator_args,
        ]
    result = subprocess.run(  # nosec B603
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    output = result.stdout + result.stderr
    for log_file in workspace.rglob("log.txt"):
        output += "\n" + log_file.read_text(errors="ignore")
    keep_dir = os.getenv("SWARMMEDHUB_SIMULATOR_OUTPUT_DIR", "").strip()
    if keep_dir:
        Path(keep_dir).mkdir(parents=True, exist_ok=True)
        Path(keep_dir, f"{workspace.name}.log").write_text(output)
    return output


def _training_lines(output: str) -> dict[tuple[str, int], tuple[bool, float]]:
    lines = {}
    for match in TEST_LINE_RE.finditer(output):
        key = (match["site"], int(match["round"]))
        lines[key] = (match["loaded"] == "True", float(match["checksum"]))
    return lines


@skipUnless(
    os.getenv("SWARMMEDHUB_RUN_FLARE_SIMULATOR") == "1",
    "set SWARMMEDHUB_RUN_FLARE_SIMULATOR=1 to run FLARE's simulator",
)
class ResumeInSimulatorTests(SubmitHarness, TestCase):
    def test_interrupted_round_is_rerun_from_the_round_start_checkpoint(self):
        # A normal four-round job (rounds 0-3).
        training_services.submit_training_job(actor=self.user, network=self.network)
        first_workspace = self.base_dir / "simulate_first"
        first_output = _run_simulator(self.submitted[0], first_workspace, self.clients)
        first_lines = _training_lines(first_output)
        self.assertIn("Swarm Learning Done: number of rounds completed 4", first_output)
        for client in self.clients:
            for round_number in range(4):
                self.assertIn((client, round_number), first_lines, first_output[-4000:])

        # Every site kept the model that round 3 started from.
        round_start = [
            c
            for c in find_resume_checkpoints(first_workspace, "simulate_job")
            if c.source == "round-start"
        ]
        self.assertEqual(sorted(c.site for c in round_start), sorted(self.clients))
        self.assertEqual({c.next_round for c in round_start}, {3})
        checkpoint = round_start[0]
        saved = torch.load(checkpoint.path, weights_only=True)["model"]
        saved_checksum = sum(float(t.double().sum()) for t in saved.values())
        self.assertAlmostEqual(
            first_lines[(self.clients[0], 3)][1], saved_checksum, places=4
        )

        # Resume as if the server had been lost during round 3, for a 6-round run.
        plan = ResumePlan(checkpoint=checkpoint, start_round=3, total_rounds=6)
        training_services.submit_training_job(
            actor=self.user, network=self.network, resume=plan
        )
        second_workspace = self.base_dir / "simulate_resumed"
        second_output = _run_simulator(
            self.submitted[1], second_workspace, self.clients
        )
        second_lines = _training_lines(second_output)

        self.assertIn(
            "Swarm Learning Done: number of rounds completed 3", second_output
        )
        for client in self.clients:
            self.assertEqual(
                sorted(r for site, r in second_lines if site == client), [3, 4, 5]
            )
            loaded, checksum = second_lines[(client, 3)]
            self.assertTrue(loaded)
            self.assertAlmostEqual(checksum, saved_checksum, places=4)
        resumed = find_resume_checkpoints(second_workspace, "simulate_job")
        self.assertEqual(
            {c.next_round for c in resumed if c.source == "round-start"}, {5}
        )
        self.assertTrue(
            all(
                Path(c.path).name in {CHECKPOINT_FILE, "FL_global_model.pt"}
                for c in resumed
            )
        )
