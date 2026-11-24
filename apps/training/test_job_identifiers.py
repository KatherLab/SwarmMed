"""The job that a training command's job identifier names.

`swarmed training status`, `watch`, `stop`, `checkpoints` and `resume` take a
record's identifier, a FLARE job ID, or the first 8 or more characters of a
FLARE job ID. The start of an ID must name exactly one job, and anything else
names none: a command that is given a job never acts on another one.
"""

from __future__ import annotations

import json
import os
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from swarmed_cli.main import main

from network.models import SwarmNetwork
from project.models import Project

from . import services as training_services
from .models import TrainingJob
from .test_resume import ServerRouting, SubmitHarness, _write_round_start_checkpoint

# The job the operator means, and other jobs at the same Hub.
JOB = "7f3b1c2d-1111-4111-8111-111111111111"
SAME_START = "7f3b1c2d-3333-4333-8333-333333333333"
OTHER = "aaaaaaaa-2222-4222-8222-222222222222"
# IDs that hold the start of JOB, or a short part of it, but do not start with it.
CONTAINS_START = "c0ffee00-1234-4567-8901-7f3b1c2d0000"
CONTAINS_PART = "c0ffee00-7f3b-4bbb-8bbb-bbbbbbbbbbbb"

# Each command that resolves a job, with the options it needs here.
COMMANDS = {
    "status": (),
    "watch": ("--timeout", "0"),
    "stop": (),
    "checkpoints": (),
    "resume": (),
}


def several_jobs(identifier: str, *jobs: str) -> str:
    return (
        f"'{identifier}' matches {len(jobs)} jobs ({', '.join(sorted(jobs))}); "
        "give more of the FLARE job ID."
    )


class JobIdentifierTests(ServerRouting, SubmitHarness, TestCase):
    def setUp(self):
        self.old_uuid = JOB
        super().setUp()
        # Leave out the note on slow links that every resume prints.
        os.environ["SWARMMEDHUB_FLARE_ADMIN_TIMEOUT"] = "300"
        self.network_id = str(self.network.identifier)
        # A status check reads the job's containers; there are none here.
        status = patch.object(
            training_services,
            "build_training_status_payload",
            side_effect=lambda network, job: {
                "status": "Running",
                "progress": 40,
                "duration": "1h",
                "eta": "1h",
                "job_id": job.flare_job_uuid,
            },
        )
        status.start()
        self.addCleanup(status.stop)

    def _record(self, flare_uuid: str, status: str = "RUNNING") -> TrainingJob:
        return TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status=status,
            flare_job_id=flare_uuid,
            flare_job_uuid=flare_uuid,
            total_rounds=8,
        )

    def _checkpoint(self, job: str, next_round: int):
        app_dir = self.client_workspace("node-A") / job / "app_node-A"
        _write_round_start_checkpoint(app_dir, next_round, "node-A")

    def _cli(self, *arguments):
        stdout = StringIO()
        with (
            # Keep the checkout's .env, which the CLI loads, out of the test.
            patch("swarmed_cli.bootstrap.load_dotenv"),
            redirect_stdout(stdout),
        ):
            code = main(["training", *arguments, "--user", "resumer", "--json"])
        return code, json.loads(stdout.getvalue())

    def _aborted(self) -> list[str]:
        return [
            call.args[0]
            for call in self.session.api.do_command.call_args_list
            if call.args and str(call.args[0]).startswith("abort_job")
        ]

    def _resumed_from(self) -> list[str]:
        path = self.network_dir / "job" / "resumed_jobs.json"
        if not path.exists():
            return []
        return [record["from_flare_job"] for record in json.loads(path.read_text())]

    def test_a_start_that_several_jobs_share_names_no_job(self):
        self._record(JOB)
        self._record(SAME_START)

        for command, options in COMMANDS.items():
            with self.subTest(command=command):
                code, envelope = self._cli(
                    command, JOB[:8], *options, "--network", self.network_id
                )

                self.assertEqual(code, 2, envelope)
                self.assertEqual(
                    envelope["errors"], [several_jobs(JOB[:8], JOB, SAME_START)]
                )
        self.assertEqual((self._aborted(), self.submitted), ([], []))

    def test_a_part_shorter_than_8_characters_names_no_job(self):
        completed = self._record(CONTAINS_PART, "COMPLETED")
        self._checkpoint(CONTAINS_PART, 7)

        for command, options in COMMANDS.items():
            with self.subTest(command=command):
                code, envelope = self._cli(
                    command, "7f3b", *options, "--network", self.network_id
                )

                self.assertEqual(code, 2, envelope)
                self.assertEqual(
                    envelope["errors"], ["Training job '7f3b' was not found."]
                )
        self.assertEqual((self._aborted(), self.submitted), ([], []))
        completed.refresh_from_db()
        self.assertEqual(completed.status, "COMPLETED")

    def test_stop_takes_the_job_whose_id_starts_with_the_identifier(self):
        job = self._record(JOB)
        # Newer, and its ID holds the identifier, but not at the start.
        inside = self._record(CONTAINS_START)

        code, envelope = self._cli("stop", JOB[:8], "--network", self.network_id)

        self.assertEqual(code, 0, envelope)
        self.assertEqual(self._aborted(), [f"abort_job {JOB}"])
        job.refresh_from_db()
        inside.refresh_from_db()
        self.assertEqual((job.status, inside.status), ("STOPPED", "RUNNING"))

    def test_resume_takes_the_job_whose_id_starts_with_the_identifier(self):
        self._record(CONTAINS_START, "COMPLETED")
        self._checkpoint(CONTAINS_START, 7)
        # The interrupted job, which this Hub has no record of.
        self._checkpoint(JOB, 3)

        code, envelope = self._cli("resume", JOB[:8], "--network", self.network_id)

        self.assertEqual(code, 0, envelope)
        self.assertEqual(self._resumed_from(), [JOB])
        self.assertEqual(self._submitted_rounds(), (3, 5))

    def test_a_record_and_a_folder_of_two_jobs_with_the_same_start_name_no_job(self):
        self._record(JOB)
        self._checkpoint(JOB, 3)
        # A job that this Hub has no record of, only its folder.
        self._checkpoint(SAME_START, 2)

        for command in ("checkpoints", "resume"):
            with self.subTest(command=command):
                code, envelope = self._cli(
                    command, JOB[:8], "--network", self.network_id
                )

                self.assertEqual(code, 2, envelope)
                self.assertEqual(
                    envelope["errors"], [several_jobs(JOB[:8], JOB, SAME_START)]
                )
        self.assertEqual(self.submitted, [])

    def test_a_job_that_is_not_found_is_never_replaced_by_another(self):
        other = self._record(OTHER)

        for name in (JOB[:8], JOB, "not-a-job"):
            for command in ("status", "stop"):
                with self.subTest(command=command, name=name):
                    code, envelope = self._cli(
                        command, name, "--network", self.network_id
                    )

                    self.assertEqual(code, 2, envelope)
                    self.assertEqual(
                        envelope["errors"], [f"Training job '{name}' was not found."]
                    )
        self.assertEqual(self._aborted(), [])
        other.refresh_from_db()
        self.assertEqual(other.status, "RUNNING")

    def test_jobs_of_projects_the_user_cannot_access_do_not_count(self):
        job = self._record(JOB)
        stranger = User.objects.create_user(
            username="stranger", password="test-password"
        )  # nosec B106
        project = Project.objects.create(title="Other Project", author=stranger)
        network = SwarmNetwork.objects.create(
            name="Other Network", project=project, author=stranger, status="RUNNING"
        )
        TrainingJob.objects.create(
            project=project,
            network=network,
            status="RUNNING",
            flare_job_id=SAME_START,
            flare_job_uuid=SAME_START,
        )

        code, envelope = self._cli("status", JOB[:8])

        self.assertEqual(code, 0, envelope)
        self.assertEqual(envelope["result"]["identifier"], str(job.identifier))

    def test_a_unique_start_a_whole_id_or_a_record_identifier_names_its_job(self):
        job = self._record(JOB)
        self._record(OTHER)

        for name in (JOB[:8], JOB[:8].upper(), JOB, str(job.identifier)):
            with self.subTest(name=name):
                code, envelope = self._cli(
                    "status", name, "--network", self.network_id
                )

                self.assertEqual(code, 0, envelope)
                self.assertEqual(envelope["result"]["identifier"], str(job.identifier))

        code, envelope = self._cli("stop", JOB[:13], "--network", self.network_id)

        self.assertEqual(code, 0, envelope)
        self.assertEqual(self._aborted(), [f"abort_job {JOB}"])

    def test_stop_without_a_job_stops_the_networks_latest_job(self):
        self._record(JOB)
        latest = self._record(OTHER)

        code, envelope = self._cli("stop", "--network", self.network_id)

        self.assertEqual(code, 0, envelope)
        self.assertEqual(self._aborted(), [f"abort_job {OTHER}"])
        latest.refresh_from_db()
        self.assertEqual(latest.status, "STOPPED")
