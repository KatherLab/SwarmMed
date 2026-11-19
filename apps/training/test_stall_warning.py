"""Tests for the warning about a running training job that has stalled.

In a test across three hospitals, one site's training process was killed and the
other sites waited for it. The FL server reported the site's job as missing every
10 s for five hours, its time limits did not end the job, and SwarmMedHub showed
the job as "Running" throughout.
"""

import json
import os
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib import admin
from django.contrib.auth.models import User
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from network.models import SwarmNetwork, SwarmParticipant
from project.models import Project, UserCurrentProject

from swarmed_cli.main import main as cli_main

from . import services as training_services
from . import stall, tasks
from .admin import TrainingJobAdmin
from .models import TrainingJob
from .runtime import get_training_progress_info
from .utils import (
    DROPOUT_DEAD,
    DROPOUT_DISCONNECTED,
    DROPOUT_MISSING,
    DROPOUT_REPORTED,
    ClientDropout,
    scan_client_dropouts,
    summarize_training_log,
)

JOB = "5a5a5a5a-1111-2222-3333-444444444444"
OTHER_JOB = "6b6b6b6b-1111-2222-3333-444444444444"
CONTEXT = f"[identity=study, run={JOB}, wf=controller]"
RUNNING_INFO = {
    "status": "Running",
    "progress": 40,
    "duration": "1h",
    "eta": "1h",
    "is_running": True,
}


def _status(when, client, round_number, action="finished_learn_task", job=JOB):
    return (
        f"{when} - SwarmServerController - INFO - [identity=study, run={job}, "
        f"wf=controller, peer={client}, peer_run={job}] - updated status of client "
        f"{client} on round {round_number}: timestamp={when}, action={action}, "
        "all_done=False"
    )


def _report(when, client, job=JOB):
    return (
        f"{when} - WFCommServer - WARNING - [identity=study, run={job}, "
        f"wf=controller] - received dead job report for client {client}"
    )


def _notice(when, client, reason="missing job on client", job=JOB):
    return (
        f"{when} - ServerEngine - WARNING - notified SJ of dead-job: "
        f"job_id='{job}'; client_name='{client}'; reason='{reason}'"
    )


def _disconnected(when, client):
    return f"{when} - WFCommServer - ERROR - Client {client} is deemed disconnected!"


def _utc(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


class DropoutLogParsingTests(SimpleTestCase):
    """Each FLARE message about a dropped-out site, and what makes it count."""

    def test_server_notices_give_the_reason(self):
        scan = scan_client_dropouts(
            "\n".join(
                [
                    _notice("2026-11-17 10:47:10,789", "node-A"),
                    _notice("2026-11-17 10:48:10,861", "node-A"),
                    _notice("2026-11-17 10:52:01,123", "node-C", "client dead"),
                ]
            ),
            JOB,
            job_log=False,
        )

        dropped = scan.dropped()
        self.assertEqual([item.client for item in dropped], ["node-A", "node-C"])
        self.assertEqual(dropped[0].reason, DROPOUT_MISSING)
        self.assertEqual(
            dropped[0].first_reported_at,
            _utc("2026-11-17 10:47:10") + timedelta(milliseconds=789),
        )
        self.assertEqual(dropped[1].reason, DROPOUT_DEAD)

    def test_job_log_reports_and_disconnects(self):
        scan = scan_client_dropouts(
            "\n".join(
                [
                    _report("2026-11-17 10:47:10,792", "node-A"),
                    f"2026-11-17 10:47:10,792 - WFCommServer - WARNING - {CONTEXT} - "
                    "client node-A is placed on dead client watch list",
                    _report("2026-11-17 10:47:20,885", "node-A"),
                    f"2026-11-17 10:47:20,885 - WFCommServer - WARNING - {CONTEXT} - "
                    "discarded dead client report client_name='node-A': already on "
                    "watch list",
                    _report("2026-11-17 10:47:30,000", "node-B"),
                    _disconnected("2026-11-17 10:48:10,861", "node-A"),
                    _report("2026-11-17 10:48:30,000", "node-B"),
                ]
            ),
            JOB,
        )

        dropped = {item.client: item for item in scan.dropped()}
        self.assertEqual(set(dropped), {"node-A", "node-B"})
        self.assertEqual(dropped["node-A"].reason, DROPOUT_DISCONNECTED)
        self.assertEqual(dropped["node-A"].first_reported_at.second, 10)
        self.assertEqual(
            dropped["node-A"].last_reported_at,
            _utc("2026-11-17 10:48:10") + timedelta(milliseconds=861),
        )
        self.assertEqual(dropped["node-B"].reason, DROPOUT_REPORTED)

    def test_other_jobs_lines_are_ignored(self):
        scan = scan_client_dropouts(
            "\n".join(
                [
                    _notice("2026-11-17 10:47:10,789", "node-B", job=OTHER_JOB),
                    _report("2026-11-17 10:47:10,792", "node-C", job=OTHER_JOB),
                    _notice("2026-11-17 10:47:20,000", "node-A"),
                    _notice("2026-11-17 10:48:20,000", "node-A"),
                    _notice("2026-11-17 10:48:30,000", "node-B", job=OTHER_JOB),
                    _report("2026-11-17 10:48:30,000", "node-C", job=OTHER_JOB),
                ]
            ),
            JOB,
        )

        self.assertEqual([item.client for item in scan.dropped()], ["node-A"])

    def test_a_log_of_several_jobs_counts_only_lines_that_name_the_job(self):
        text = "\n".join(
            [
                _disconnected("2026-11-17 10:48:10,861", "node-B"),
                _notice("2026-11-17 10:47:10,789", "node-A"),
                _notice("2026-11-17 10:48:20,882", "node-A"),
            ]
        )

        own_log = scan_client_dropouts(text, JOB, job_log=True)
        shared_log = scan_client_dropouts(text, JOB, job_log=False)

        self.assertEqual(
            {item.client for item in own_log.dropped()}, {"node-A", "node-B"}
        )
        self.assertEqual([item.client for item in shared_log.dropped()], ["node-A"])

    def test_lines_without_a_time_are_skipped(self):
        scan = scan_client_dropouts(
            "received dead job report for client node-A\n", JOB
        )

        self.assertEqual(scan.dropped(), [])

    def test_a_site_that_takes_part_again_is_not_dropped(self):
        # Reports for a minute while a site's job was still starting, then its
        # first status.
        scan = scan_client_dropouts(
            "\n".join(
                [
                    _report("2026-11-17 08:59:05,000", "node-A"),
                    _report("2026-11-17 09:00:05,000", "node-A"),
                    _status("2026-11-17 09:00:30,000", "node-A", 0, "start_learn_task"),
                    _report("2026-11-17 09:00:40,000", "node-B"),
                    _report("2026-11-17 09:01:40,000", "node-B"),
                    f"2026-11-17 09:01:50,000 - WFCommServer - INFO - {CONTEXT} - "
                    "Client node-B is removed from watch list: getTask",
                    _disconnected("2026-11-17 09:02:00,000", "node-C"),
                    f"2026-11-17 09:03:00,000 - WFCommServer - INFO - {CONTEXT} - "
                    "Client node-C is reconnected",
                ]
            ),
            JOB,
        )

        self.assertEqual(scan.dropped(), [])

    def test_a_missing_job_counts_once_its_reports_span_a_minute(self):
        # FLARE can report a site's job as missing once while the job's process
        # is still starting, and deems the site disconnected only 60 s later.
        def dropped(*lines):
            scan = scan_client_dropouts("\n".join(lines), JOB)
            return [item.client for item in scan.dropped()]

        first = _notice("2026-11-17 10:47:10,789", "node-A")

        self.assertEqual(dropped(first), [])
        self.assertEqual(
            dropped(first, _report("2026-11-17 10:48:10,788", "node-A")), []
        )
        self.assertEqual(
            dropped(first, _report("2026-11-17 10:48:10,789", "node-A")), ["node-A"]
        )
        # A dead FL client, or a site that FLARE deems disconnected, counts at once.
        self.assertEqual(
            dropped(_notice("2026-11-18 01:02:01,123", "node-C", "client dead")),
            ["node-C"],
        )
        self.assertEqual(
            dropped(_disconnected("2026-11-17 10:48:10,861", "node-A")), ["node-A"]
        )

    def test_a_round_the_site_had_sent_its_result_for_does_not_end_its_dropout(self):
        # node-C's FL client stops after node-C has sent its result for round 4.
        # FLARE reports the dead client once; node-A and node-B still finish
        # round 4 and then wait for node-C in round 5.
        job_log = scan_client_dropouts(
            "\n".join(
                [
                    _status("2026-11-18 00:50:41,000", "node-C", 4, "start_learn_task"),
                    _report("2026-11-18 01:02:01,126", "node-C"),
                    _disconnected("2026-11-18 01:03:01,200", "node-C"),
                    _status("2026-11-18 01:04:00,000", "node-A", 4),
                    _status("2026-11-18 01:04:05,000", "node-B", 4),
                    _status("2026-11-18 01:04:10,000", "node-A", 5, "start_learn_task"),
                ]
            ),
            JOB,
        )
        server_log = scan_client_dropouts(
            _notice("2026-11-18 01:02:01,123", "node-C", "client dead"),
            JOB,
            job_log=False,
        )

        dropped = job_log.merge(server_log).dropped()

        self.assertEqual([item.client for item in dropped], ["node-C"])
        self.assertEqual(dropped[0].reason, DROPOUT_DEAD)

    def test_training_by_a_site_on_the_server_hub_does_not_end_a_dropout(self):
        # The Hub that runs the FL server also runs node-B, whose job log has a
        # finished local round 8 s after the latest report about node-A.
        server_job_log = scan_client_dropouts(
            "\n".join(
                [
                    _status("2026-11-17 10:46:53,214", "node-B", 1, "start_learn_task"),
                    _report("2026-11-17 10:47:10,792", "node-A"),
                    _report("2026-11-17 10:52:21,000", "node-A"),
                ]
            ),
            JOB,
        )
        client_job_log = scan_client_dropouts(
            "2026-11-17 10:52:29,143 - SwarmClientController - INFO - "
            f"[identity=node-B, run={JOB}, peer=node-A, peer_run={JOB}, "
            "task_name=swarm_learn] - finished training round 1",
            JOB,
        )

        dropped = server_job_log.merge(client_job_log).dropped()

        self.assertEqual([item.client for item in dropped], ["node-A"])

    def test_repeated_reports_outlast_the_progress_of_the_waiting_sites(self):
        lines = [_status("2026-11-17 10:40:00,000", "node-A", 3, "start_learn_task")]
        lines += [
            _report(f"2026-11-17 10:{minute}:10,000", "node-A")
            for minute in range(47, 60)
        ]
        lines.insert(5, _status("2026-11-17 10:50:30,000", "node-B", 3))

        dropped = scan_client_dropouts("\n".join(lines), JOB).dropped()

        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0].first_reported_at, _utc("2026-11-17 10:47:10"))
        self.assertEqual(dropped[0].last_reported_at, _utc("2026-11-17 10:59:10"))

    def test_merging_the_server_and_job_logs_keeps_the_most_specific_reason(self):
        job_log = scan_client_dropouts(
            "\n".join(
                [
                    _report("2026-11-17 10:47:10,792", "node-A"),
                    _disconnected("2026-11-17 10:48:10,861", "node-A"),
                ]
            ),
            JOB,
        )
        server_log = scan_client_dropouts(
            _notice("2026-11-17 10:47:10,789", "node-A"), JOB, job_log=False
        )

        dropped = job_log.merge(server_log).merge(None).dropped()

        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0].reason, DROPOUT_MISSING)
        self.assertEqual(dropped[0].first_reported_at.microsecond, 789000)

    def test_summary_includes_the_dropouts_of_its_job(self):
        summary = summarize_training_log(
            "\n".join(
                [
                    _status("2026-11-17 10:44:56,873", "node-B", 3),
                    _report("2026-11-17 10:47:10,792", "node-A"),
                    _report("2026-11-17 10:47:10,800", "node-C", job=OTHER_JOB),
                    _report("2026-11-17 10:48:20,885", "node-A"),
                    _report("2026-11-17 10:48:20,890", "node-C", job=OTHER_JOB),
                ]
            ),
            JOB,
        )

        self.assertEqual(summary["rounds_finished"], 3)
        self.assertIsNone(summary["terminal_status"])
        self.assertEqual(
            [item.client for item in summary["client_dropouts"].dropped()], ["node-A"]
        )


class StallRuleTests(SimpleTestCase):
    """The message for each kind of stall, at given times."""

    def setUp(self):
        self.now = _utc("2026-11-17 12:00:00")

    def _dropout(self, client, reason, first):
        return ClientDropout(
            client=client,
            reason=reason,
            first_reported_at=_utc(first),
            last_reported_at=self.now,
        )

    def test_a_dropped_out_site_is_named_with_what_to_do(self):
        message = stall.describe_stall(
            dropouts=[self._dropout("node-A", DROPOUT_MISSING, "2026-11-17 10:47:10")],
            last_round_at=self.now,
            now=self.now,
            threshold_minutes=60,
        )

        self.assertEqual(
            message,
            "Site node-A dropped out at 2026-11-17 10:47 UTC: the FL server reports "
            "its training job as missing. The other sites are waiting; check the "
            "site, then stop this job and resume it (swarmed training resume).",
        )

    def test_several_dropped_out_sites(self):
        message = stall.describe_stall(
            dropouts=[
                self._dropout("node-A", DROPOUT_MISSING, "2026-11-17 10:47:10"),
                self._dropout("node-C", DROPOUT_DEAD, "2026-11-17 11:02:01"),
            ],
            last_round_at=None,
            now=self.now,
            threshold_minutes=0,
        )

        self.assertIn("Site node-A dropped out at 2026-11-17 10:47 UTC", message)
        self.assertIn(
            "Site node-C dropped out at 2026-11-17 11:02 UTC: the FL server has lost "
            "contact with its FL client.",
            message,
        )
        self.assertIn("check these sites", message)

    def test_no_finished_round_for_longer_than_the_threshold(self):
        last_round_at = _utc("2026-11-17 10:46:00")

        self.assertEqual(
            stall.describe_stall(
                dropouts=[],
                last_round_at=last_round_at,
                now=last_round_at + timedelta(minutes=59),
                threshold_minutes=60,
            ),
            "",
        )
        self.assertEqual(
            stall.describe_stall(
                dropouts=[],
                last_round_at=last_round_at,
                now=last_round_at + timedelta(minutes=75),
                threshold_minutes=60,
            ),
            "No round has finished since 2026-11-17 10:46 UTC (more than 60 min). "
            "Check that every site is still training; if one has dropped out, stop "
            "this job and resume it (swarmed training resume).",
        )

    def test_zero_turns_the_rounds_check_off(self):
        self.assertEqual(
            stall.describe_stall(
                dropouts=[],
                last_round_at=self.now - timedelta(days=2),
                now=self.now,
                threshold_minutes=0,
            ),
            "",
        )

    def test_a_job_without_a_round_time_is_not_flagged(self):
        self.assertEqual(
            stall.describe_stall(
                dropouts=[], last_round_at=None, now=self.now, threshold_minutes=60
            ),
            "",
        )

    def test_a_threshold_longer_than_a_timedelta_can_hold(self):
        self.assertEqual(
            stall.describe_stall(
                dropouts=[],
                last_round_at=self.now - timedelta(days=2),
                now=self.now,
                threshold_minutes=1e13,
            ),
            "",
        )

    def test_warnings_that_differ_only_in_their_times_are_the_same(self):
        first = stall.describe_stall(
            dropouts=[self._dropout("node-A", DROPOUT_MISSING, "2026-11-17 10:47:10")],
            last_round_at=None,
            now=self.now,
            threshold_minutes=60,
        )
        drifted = first.replace("10:47", "14:35")
        other_site = first.replace("node-A", "node-B")

        self.assertTrue(stall.same_stall(first, drifted))
        self.assertFalse(stall.same_stall(first, other_site))
        self.assertFalse(stall.same_stall("", ""))
        self.assertFalse(stall.same_stall(first, ""))

    def test_warnings_about_the_same_sites_are_the_same_stall(self):
        def warning(*dropouts, last_round_at=None, threshold_minutes=60):
            return stall.describe_stall(
                dropouts=list(dropouts),
                last_round_at=last_round_at,
                now=self.now,
                threshold_minutes=threshold_minutes,
            )

        dead = warning(self._dropout("node-C", DROPOUT_DEAD, "2026-11-17 11:02:01"))
        # The one-time notice has left the end of the server's log.
        fallen_back = warning(
            self._dropout("node-C", DROPOUT_DISCONNECTED, "2026-11-17 11:02:01")
        )
        another_site_too = warning(
            self._dropout("node-C", DROPOUT_DEAD, "2026-11-17 11:02:01"),
            self._dropout("node-A", DROPOUT_MISSING, "2026-11-17 11:30:00"),
        )
        no_round = warning(last_round_at=self.now - timedelta(minutes=90))

        self.assertTrue(stall.same_stall(dead, fallen_back))
        self.assertFalse(stall.same_stall(dead, another_site_too))
        self.assertFalse(stall.same_stall(dead, no_round))
        self.assertFalse(stall.same_stall(no_round, dead))
        self.assertTrue(
            stall.same_stall(
                no_round, warning(last_round_at=self.now - timedelta(minutes=80))
            )
        )
        self.assertFalse(
            stall.same_stall(
                no_round,
                warning(
                    last_round_at=self.now - timedelta(minutes=90),
                    threshold_minutes=30,
                ),
            )
        )

    def test_warnings_about_the_same_sites_in_another_order_are_the_same_stall(self):
        # Sites are listed by their first report among the lines read, and which
        # site's early reports leave the end of the logs first can vary.
        node_a = self._dropout("node-A", DROPOUT_MISSING, "2026-11-17 10:47:10")
        node_b = self._dropout("node-B", DROPOUT_MISSING, "2026-11-17 10:47:40")

        def warning(*dropouts):
            return stall.describe_stall(
                dropouts=list(dropouts),
                last_round_at=None,
                now=self.now,
                threshold_minutes=60,
            )

        first = warning(node_a, node_b)

        self.assertTrue(stall.same_stall(first, warning(node_b, node_a)))
        self.assertFalse(stall.same_stall(first, warning(node_b)))


class StallWarningMinutesTests(SimpleTestCase):
    def setUp(self):
        stall._reported_invalid_values.clear()
        self.addCleanup(stall._reported_invalid_values.clear)

    def _minutes(self, value, log=None):
        with patch.dict(os.environ, {stall.STALL_WARNING_ENV: value}):
            return stall.stall_warning_minutes(log)

    def test_default_valid_and_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(stall.STALL_WARNING_ENV, None)
            self.assertEqual(stall.stall_warning_minutes(), 60)
        self.assertEqual(self._minutes("90"), 90)
        self.assertEqual(self._minutes(" 7.5 "), 7.5)
        self.assertEqual(self._minutes("0"), 0)

    def test_invalid_values_fall_back_to_60_with_one_warning_each(self):
        log = MagicMock()
        for value in ("an hour", "-5", "nan", "inf"):
            with self.subTest(value=value):
                self.assertEqual(self._minutes(value, log), 60)
        self.assertEqual(self._minutes("-5", log), 60)

        self.assertEqual(log.training.warning.call_count, 4)
        self.assertIn(
            "SWARMMEDHUB_STALL_WARNING_MINUTES='an hour'",
            log.training.warning.call_args_list[0].args[0],
        )


class PersistStallStateTests(TestCase):
    """last_round_at and the warning as persist_training_job_state keeps them."""

    def setUp(self):
        user = User.objects.create_user(
            username="stall", password="test-password"
        )  # nosec B106
        project = Project.objects.create(title="Stall Project", author=user)
        self.network = SwarmNetwork.objects.create(
            name="Stall Network", project=project, author=user, status="RUNNING"
        )
        self.job = TrainingJob.objects.create(
            project=project, network=self.network, status="PENDING"
        )

    def test_starting_to_run_sets_the_round_time(self):
        before = timezone.now()
        training_services.persist_training_job_state(self.job, status="RUNNING")
        self.job.refresh_from_db()

        self.assertGreaterEqual(self.job.last_round_at, before)

    def test_a_new_round_moves_the_round_time_and_clears_the_warning(self):
        training_services.persist_training_job_state(
            self.job, status="RUNNING", rounds_finished=2, stall_warning="stalled"
        )
        self.job.last_round_at = timezone.now() - timedelta(hours=2)
        self.job.save(update_fields=["last_round_at"])

        training_services.persist_training_job_state(self.job, rounds_finished=2)
        self.job.refresh_from_db()
        self.assertEqual(self.job.stall_warning, "stalled")

        training_services.persist_training_job_state(self.job, rounds_finished=3)
        self.job.refresh_from_db()
        self.assertEqual(self.job.stall_warning, "")
        self.assertGreater(
            self.job.last_round_at, timezone.now() - timedelta(minutes=1)
        )

    def test_a_new_round_leaves_a_warning_about_a_site_to_the_monitor(self):
        # The other sites can finish a round for which the site had sent its
        # result; only the job monitor can tell that the site takes part again.
        warning = (
            "Site node-C dropped out at 2026-11-18 01:02 UTC: the FL server has "
            "lost contact with its FL client."
        )
        training_services.persist_training_job_state(
            self.job, status="RUNNING", rounds_finished=3, stall_warning=warning
        )

        training_services.persist_training_job_state(self.job, rounds_finished=4)
        self.job.refresh_from_db()
        self.assertEqual(self.job.rounds_finished, 4)
        self.assertEqual(self.job.stall_warning, warning)

        training_services.persist_training_job_state(self.job, status="STOPPED")
        self.job.refresh_from_db()
        self.assertEqual(self.job.stall_warning, "")

    def test_an_ended_job_has_no_warning(self):
        training_services.persist_training_job_state(
            self.job, status="RUNNING", stall_warning="stalled"
        )
        training_services.persist_training_job_state(self.job, status="STOPPED")
        self.job.refresh_from_db()

        self.assertEqual(self.job.status, "STOPPED")
        self.assertEqual(self.job.stall_warning, "")

    def test_a_job_that_ends_loses_a_warning_set_after_it_was_read(self):
        training_services.persist_training_job_state(self.job, status="RUNNING")
        read_by_a_stop_request = TrainingJob.objects.get(pk=self.job.pk)
        # The monitor flags the job while the stop request aborts it.
        TrainingJob.objects.filter(pk=self.job.pk).update(stall_warning="stalled")

        training_services.persist_training_job_state(
            read_by_a_stop_request, status="STOPPED"
        )
        self.job.refresh_from_db()

        self.assertEqual(self.job.status, "STOPPED")
        self.assertEqual(self.job.stall_warning, "")

    def test_only_a_running_job_takes_a_warning(self):
        training_services.persist_training_job_state(self.job, stall_warning="stalled")
        self.job.refresh_from_db()

        self.assertEqual(self.job.stall_warning, "")

    def test_payload_and_serialized_job_give_the_warning(self):
        training_services.persist_training_job_state(
            self.job, status="RUNNING", stall_warning="Site node-A dropped out."
        )
        with patch(
            "training.services.build_training_status_payload",
            return_value={"status": "Running", "progress": 40, "job_id": "x"},
        ):
            payload = training_services.get_training_status_payload(
                network=self.network, job=self.job
            )
        with patch(
            "training.services.get_training_progress_info", return_value=RUNNING_INFO
        ):
            serialized = training_services.serialize_training_job(self.job)

        for result in (payload, serialized):
            self.assertEqual(result["stall_warning"], "Site node-A dropped out.")
            self.assertEqual(
                result["last_round_at"], self.job.last_round_at.isoformat()
            )

    def test_payload_of_a_job_that_ended_has_no_warning(self):
        training_services.persist_training_job_state(
            self.job, status="RUNNING", stall_warning="Site node-A dropped out."
        )
        with patch(
            "training.services.build_training_status_payload",
            return_value={"status": "Failed", "progress": 40, "job_id": "x"},
        ):
            payload = training_services.get_training_status_payload(
                network=self.network, job=self.job
            )

        self.assertEqual(payload["stall_warning"], "")

    def test_payload_without_a_job_has_the_keys(self):
        self.job.delete()
        with patch(
            "training.services.build_training_status_payload",
            return_value={"status": "idle", "progress": 0},
        ):
            payload = training_services.get_training_status_payload(
                network=self.network, job=None
            )

        self.assertIsNone(payload["last_round_at"])
        self.assertEqual(payload["stall_warning"], "")


class MonitorTestCase(TestCase):
    """A Hub with the FL server's logs of a running job, and its job monitor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        user = User.objects.create_user(
            username="monitor", password="test-password"
        )  # nosec B106
        self.project = Project.objects.create(title="Monitor Project", author=user)
        self.network = SwarmNetwork.objects.create(
            name="Monitor Network", project=self.project, author=user, status="RUNNING"
        )
        self.prod = (
            Path(self.tmp.name)
            / "workspaces"
            / str(self.project.identifier)
            / str(self.network.identifier)
            / "workspace"
            / "study"
            / "prod_00"
        )
        self.server = self.prod / "server"
        (self.server / "startup").mkdir(parents=True)
        (self.server / "startup" / "fed_server.json").write_text("{}")
        (self.server / JOB / "app_server").mkdir(parents=True)

        self.log = MagicMock()
        for target, value in (
            (
                "training.tasks.logger",
                MagicMock(get_logger=MagicMock(return_value=self.log)),
            ),
            (
                "network.overlay.get_local_addresses",
                MagicMock(return_value=frozenset({"127.0.0.1"})),
            ),
            ("training.utils.scrape_docker_progress", MagicMock(return_value=[])),
            (
                "training.tasks.resolve_admin_session_target",
                MagicMock(return_value=None),
            ),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {stall.STALL_WARNING_ENV: "60"})
        env.start()
        self.addCleanup(env.stop)

    def _job(self, **fields):
        values = {
            "project": self.project,
            "network": self.network,
            "status": "RUNNING",
            "flare_job_id": JOB,
            "flare_job_uuid": JOB,
            "total_rounds": 10,
            "rounds_finished": 3,
            "last_round_at": timezone.now(),
        }
        values.update(fields)
        return TrainingJob.objects.create(**values)

    def _write_job_log(self, *lines):
        (self.server / JOB / "log.txt").write_text("\n".join(lines) + "\n")

    def _write_server_log(self, *lines):
        (self.server / "log.txt").write_text("\n".join(lines) + "\n")

    def _run_monitor(self):
        with override_settings(BASE_DIR=self.tmp.name):
            tasks.monitor_training_jobs()

    def _run_monitor_at(self, when):
        with patch("django.utils.timezone.now", return_value=when):
            self._run_monitor()


class MonitorStallWarningTests(MonitorTestCase):
    """The periodic monitor sets and clears the warning from the logs at a Hub."""

    def test_a_dropped_out_site_sets_the_warning_and_keeps_the_status(self):
        job = self._job()
        self._write_job_log(
            _status("2026-11-17 10:40:00,000", "node-A", 3, "start_learn_task"),
            _status("2026-11-17 10:44:56,873", "node-B", 3),
            _report("2026-11-17 10:47:10,792", "node-A"),
            _report("2026-11-17 10:47:20,885", "node-A"),
            _disconnected("2026-11-17 10:48:10,861", "node-A"),
        )
        self._write_server_log(
            _notice("2026-11-17 10:47:10,789", "node-A"),
            _notice("2026-11-17 10:47:15,000", "node-B", job=OTHER_JOB),
            _notice("2026-11-17 10:47:20,882", "node-A"),
        )

        self._run_monitor()
        job.refresh_from_db()

        self.assertEqual(job.status, "RUNNING")
        self.assertEqual(
            job.stall_warning,
            "Site node-A dropped out at 2026-11-17 10:47 UTC: the FL server reports "
            "its training job as missing. The other sites are waiting; check the "
            "site, then stop this job and resume it (swarmed training resume).",
        )
        self.log.training.warning.assert_called_once()
        self.assertIn(str(job.identifier), self.log.training.warning.call_args.args[0])

    def test_without_the_server_log_the_job_log_suffices(self):
        job = self._job()
        self._write_job_log(
            _report("2026-11-17 10:47:10,792", "node-A"),
            _disconnected("2026-11-17 10:48:10,861", "node-A"),
        )

        self._run_monitor()
        job.refresh_from_db()

        self.assertIn("the FL server reports it as disconnected", job.stall_warning)

    def test_the_time_shown_first_is_kept_when_early_reports_leave_the_log_end(self):
        shown = (
            "Site node-A dropped out at 2026-11-17 10:47 UTC: the FL server reports "
            "its training job as missing. The other sites are waiting; check the "
            "site, then stop this job and resume it (swarmed training resume)."
        )
        job = self._job(stall_warning=shown)
        self._write_server_log(
            _notice("2026-11-17 14:34:24,155", "node-A"),
            _notice("2026-11-17 14:35:24,155", "node-A"),
        )

        self._run_monitor()
        job.refresh_from_db()

        self.assertEqual(job.stall_warning, shown)
        self.log.training.warning.assert_not_called()

    def test_the_warning_given_first_is_kept_when_its_sites_change_places(self):
        shown = (
            "Site node-A dropped out at 2026-11-17 10:47 UTC: the FL server reports "
            "its training job as missing. Site node-B dropped out at 2026-11-17 "
            "10:47 UTC: the FL server reports its training job as missing. The "
            "other sites are waiting; check these sites, then stop this job and "
            "resume it (swarmed training resume)."
        )
        job = self._job(stall_warning=shown)
        # Hours later the earliest report left among the lines read is node-B's.
        self._write_server_log(
            _notice("2026-11-17 14:34:20,000", "node-B"),
            _notice("2026-11-17 14:34:24,155", "node-A"),
            _notice("2026-11-17 14:35:20,000", "node-B"),
            _notice("2026-11-17 14:35:24,155", "node-A"),
        )

        self._run_monitor()
        job.refresh_from_db()

        self.assertEqual(job.stall_warning, shown)
        self.log.training.warning.assert_not_called()

    def test_a_site_whose_job_is_still_starting_is_not_named(self):
        # FLARE can report a site's job as missing once before the job's process
        # at the site is up.
        job = self._job(rounds_finished=None)
        self._write_server_log(_notice("2026-11-17 10:20:23,837", "node-B"))

        self._run_monitor()
        job.refresh_from_db()
        self.assertEqual(job.stall_warning, "")

        self._write_job_log(
            _status("2026-11-17 10:20:35,000", "node-B", 0, "start_learn_task")
        )
        self._run_monitor()
        job.refresh_from_db()

        self.assertEqual(job.stall_warning, "")
        self.log.training.warning.assert_not_called()

    def test_no_round_for_longer_than_the_threshold(self):
        last_round_at = timezone.now()
        job = self._job(last_round_at=last_round_at)
        self._write_job_log(_status("2026-11-17 10:44:56,873", "node-B", 3))

        with patch(
            "django.utils.timezone.now",
            return_value=last_round_at + timedelta(minutes=59),
        ):
            self._run_monitor()
        job.refresh_from_db()
        self.assertEqual(job.stall_warning, "")

        with patch(
            "django.utils.timezone.now",
            return_value=last_round_at + timedelta(minutes=61),
        ):
            self._run_monitor()
        job.refresh_from_db()
        self.assertTrue(job.stall_warning.startswith("No round has finished since "))
        self.assertEqual(job.status, "RUNNING")

        with patch.dict(os.environ, {stall.STALL_WARNING_ENV: "0"}):
            self._run_monitor()
        job.refresh_from_db()
        self.assertEqual(job.stall_warning, "")

    def test_rounds_that_advance_clear_the_warning(self):
        job = self._job(
            stall_warning="Site node-A dropped out at 2026-11-17 10:47 UTC: ...",
            last_round_at=timezone.now() - timedelta(hours=2),
        )
        self._write_job_log(
            _report("2026-11-17 10:47:10,792", "node-A"),
            _report("2026-11-17 10:48:10,861", "node-A"),
            _status("2026-11-17 11:30:00,000", "node-A", 4, "start_learn_task"),
            _status("2026-11-17 11:35:00,000", "node-B", 4),
        )

        self._run_monitor()
        job.refresh_from_db()

        self.assertEqual(job.rounds_finished, 4)
        self.assertEqual(job.stall_warning, "")
        self.assertGreater(job.last_round_at, timezone.now() - timedelta(minutes=1))
        self.log.training.info.assert_any_call(
            f"Training job {job.identifier} is no longer stalled.",
            project=job.project,
            network=job.network,
        )

    def test_a_job_that_ends_loses_its_warning(self):
        job = self._job(
            stall_warning="Site node-A dropped out at 2026-11-17 10:47 UTC: ..."
        )
        self._write_job_log(
            _report("2026-11-17 10:47:10,792", "node-A"),
            f"2026-11-17 12:00:00,000 - ServerRunner - ERROR - {CONTEXT} - Aborting "
            "current RUN due to FATAL_SYSTEM_ERROR received: client node-A did not "
            "report",
        )

        self._run_monitor()
        job.refresh_from_db()

        self.assertEqual(job.status, "FAILED")
        self.assertEqual(job.stall_warning, "")

    def test_a_round_finished_after_a_site_died_keeps_its_warning(self):
        job = self._job()
        before_the_round_ends = [
            _status("2026-11-18 00:50:43,000", "node-C", 4, "start_learn_task"),
            _report("2026-11-18 01:02:01,126", "node-C"),
            _disconnected("2026-11-18 01:03:01,200", "node-C"),
        ]
        self._write_job_log(*before_the_round_ends)
        self._write_server_log(
            _notice("2026-11-18 01:02:01,123", "node-C", "client dead")
        )

        self._run_monitor()
        job.refresh_from_db()
        flagged = job.stall_warning
        self.assertTrue(
            flagged.startswith(
                "Site node-C dropped out at 2026-11-18 01:02 UTC: the FL server has "
                "lost contact with its FL client."
            )
        )

        # node-A and node-B finish round 4, for which node-C had sent its result.
        self._write_job_log(
            *before_the_round_ends,
            _status("2026-11-18 01:04:00,000", "node-A", 4),
            _status("2026-11-18 01:04:05,000", "node-B", 4),
        )
        self._run_monitor()
        job.refresh_from_db()

        self.assertEqual(job.rounds_finished, 4)
        self.assertEqual(job.stall_warning, flagged)
        self.log.training.warning.assert_called_once()
        self.assertNotIn(
            f"Training job {job.identifier} is no longer stalled.",
            [call.args[0] for call in self.log.training.info.call_args_list],
        )

    def test_rounds_seen_first_in_the_container_logs_keep_the_warning_quiet(self):
        shown = (
            "Site node-C dropped out at 2026-11-18 01:02 UTC: the FL server has lost "
            "contact with its FL client. The other sites are waiting; check the "
            "site, then stop this job and resume it (swarmed training resume)."
        )
        job = self._job(stall_warning=shown)
        self._write_job_log(
            _status("2026-11-18 00:50:43,000", "node-C", 4, "start_learn_task"),
            _report("2026-11-18 01:02:01,126", "node-C"),
            _disconnected("2026-11-18 01:03:01,200", "node-C"),
            _status("2026-11-18 01:04:00,000", "node-A", 4),
            _status("2026-11-18 01:04:05,000", "node-B", 4),
        )
        self._write_server_log(
            _notice("2026-11-18 01:02:01,123", "node-C", "client dead")
        )
        # The monitor's scrape of the local containers sees round 4 finish first.
        scraped = {"job_id": JOB, "rounds_finished": 4, "terminal_status": None}

        with patch("training.utils.scrape_docker_progress", return_value=[scraped]):
            self._run_monitor()
        job.refresh_from_db()

        self.assertEqual(job.rounds_finished, 4)
        self.assertEqual(job.stall_warning, shown)
        self.log.training.warning.assert_not_called()

    def test_rounds_seen_first_in_the_container_logs_end_a_rounds_warning(self):
        job = self._job(
            stall_warning="No round has finished since 2026-11-17 10:46 UTC (more "
            "than 60 min). Check that every site is still training; if one has "
            "dropped out, stop this job and resume it (swarmed training resume).",
            last_round_at=timezone.now() - timedelta(hours=2),
        )
        self._write_job_log(_status("2026-11-17 11:35:00,000", "node-B", 4))
        scraped = {"job_id": JOB, "rounds_finished": 4, "terminal_status": None}

        with patch("training.utils.scrape_docker_progress", return_value=[scraped]):
            self._run_monitor()
        job.refresh_from_db()

        self.assertEqual(job.stall_warning, "")
        self.log.training.info.assert_any_call(
            f"Training job {job.identifier} is no longer stalled.",
            project=job.project,
            network=job.network,
        )

    def test_the_warning_given_first_is_kept_when_its_reason_leaves_the_log_end(self):
        shown = (
            "Site node-C dropped out at 2026-11-18 01:02 UTC: the FL server has lost "
            "contact with its FL client. The other sites are waiting; check the "
            "site, then stop this job and resume it (swarmed training resume)."
        )
        job = self._job(stall_warning=shown)
        # Hours later the one-time notice is no longer among the lines read from
        # the server's own log; the job's log still has the report.
        self._write_job_log(
            _status("2026-11-18 00:50:43,000", "node-C", 4, "start_learn_task"),
            _report("2026-11-18 01:02:01,126", "node-C"),
            _disconnected("2026-11-18 01:03:01,200", "node-C"),
        )
        self._write_server_log(
            "2026-11-18 04:00:00,000 - conn_manager - INFO - Connection closed"
        )

        self._run_monitor()
        job.refresh_from_db()

        self.assertEqual(job.stall_warning, shown)
        self.log.training.warning.assert_not_called()

    def test_a_job_stopped_during_the_pass_gets_no_warning(self):
        job = self._job()
        self._write_job_log(
            _report("2026-11-17 10:47:10,792", "node-A"),
            _report("2026-11-17 10:48:10,861", "node-A"),
        )

        def stopped_meanwhile(**kwargs):
            # Stop Training is pressed while the monitor reads the logs.
            training_services.persist_training_job_state(
                TrainingJob.objects.get(pk=job.pk), status="STOPPED"
            )
            return stall.describe_stall(**kwargs)

        with patch("training.tasks.describe_stall", side_effect=stopped_meanwhile):
            self._run_monitor()
        job.refresh_from_db()

        self.assertEqual(job.status, "STOPPED")
        self.assertEqual(job.stall_warning, "")
        self.log.training.warning.assert_not_called()

    def test_finishing_round_0_counts_as_a_finished_round(self):
        started_at = timezone.now()
        job = self._job(status="PENDING", rounds_finished=None, last_round_at=None)
        with patch("django.utils.timezone.now", return_value=started_at):
            training_services.persist_training_job_state(job, status="RUNNING")
        round_0_started = _status(
            "2026-11-17 10:00:00,000", "node-A", 0, "start_learn_task"
        )
        self._write_job_log(round_0_started)

        self._run_monitor_at(started_at + timedelta(minutes=1))
        job.refresh_from_db()
        self.assertIsNone(job.rounds_finished)
        self.assertEqual(job.progress_percent, 0)
        self.assertEqual(job.last_round_at, started_at)

        self._write_job_log(
            round_0_started,
            _status("2026-11-17 10:40:00,000", "node-A", 0),
            _status("2026-11-17 10:40:05,000", "node-B", 0),
        )
        round_0_seen_at = started_at + timedelta(minutes=41)
        self._run_monitor_at(round_0_seen_at)
        job.refresh_from_db()
        self.assertEqual(job.rounds_finished, 0)
        self.assertEqual(job.progress_percent, 10)
        self.assertEqual(job.last_round_at, round_0_seen_at)

        # Round 1 has run for 21 minutes.
        self._run_monitor_at(started_at + timedelta(minutes=62))
        job.refresh_from_db()
        self.assertEqual(job.stall_warning, "")

        self._run_monitor_at(round_0_seen_at + timedelta(minutes=61))
        job.refresh_from_db()
        self.assertTrue(
            job.stall_warning.startswith(
                "No round has finished since "
                f"{round_0_seen_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
            )
        )


class StatusAPIStallWarningTests(MonitorTestCase):
    """The Training page's status polls leave a warning about a site in place.

    The page and the dashboard poll the training status API every few seconds. It
    records the rounds that the local containers and the other Hubs report, and a
    round can finish after a site has dropped out: the other sites can still finish
    a round for which the site had sent its result.
    """

    WARNING = (
        "Site node-C dropped out at 2026-11-18 01:02 UTC: the FL server has lost "
        "contact with its FL client. The other sites are waiting; check the site, "
        "then stop this job and resume it (swarmed training resume)."
    )
    BEFORE_THE_ROUND_ENDS = (
        _status("2026-11-18 00:50:43,000", "node-C", 4, "start_learn_task"),
        _report("2026-11-18 01:02:01,126", "node-C"),
        _disconnected("2026-11-18 01:03:01,200", "node-C"),
    )

    def setUp(self):
        super().setUp()
        environ = patch.dict(os.environ, {"SWARMMEDHUB_DISABLE_ASYNC_LOGGING": "1"})
        environ.start()
        self.addCleanup(environ.stop)
        self.user = User.objects.get(username="monitor")
        self.user.profile.accepted_terms = True
        self.user.profile.accepted_policy = True
        self.user.profile.save(update_fields=["accepted_terms", "accepted_policy"])
        UserCurrentProject.objects.create(user=self.user, project=self.project)
        # This Hub runs the FL server, the only Hub that can name a site.
        SwarmParticipant.objects.create(
            network=self.network,
            user=self.user,
            role="SERVER",
            participant_id="server",
            ip="127.0.0.1",
        )
        self.client.force_login(self.user)

        self.job = self._job()
        self._write_job_log(*self.BEFORE_THE_ROUND_ENDS)
        self._write_server_log(
            _notice("2026-11-18 01:02:01,123", "node-C", "client dead")
        )
        self._run_monitor()
        self.job.refresh_from_db()
        self.assertEqual(self.job.stall_warning, self.WARNING)

    def _poll(self, *, scraped=(), peer_job=None):
        peer_response = MagicMock(status_code=200)
        peer_response.json.return_value = {"job": peer_job}
        with override_settings(BASE_DIR=self.tmp.name), patch(
            "training.utils.scrape_docker_progress", return_value=list(scraped)
        ), patch(
            "training.runtime.summarize_runtime_container_logs", return_value=None
        ), patch(
            "training.views._nvflare_status_payload", return_value=None
        ), patch(
            "training.views.requests.get", return_value=peer_response
        ) as peer_get:
            response = self.client.get(reverse("training:training_status_api"))
        return response, peer_get

    def _assert_the_warning_stays_and_is_logged_once(self, response):
        self.job.refresh_from_db()
        self.assertEqual(self.job.rounds_finished, 4)
        self.assertEqual(self.job.stall_warning, self.WARNING)
        self.assertEqual(response.json()["stall_warning"], self.WARNING)

        self._run_monitor()
        self.job.refresh_from_db()

        self.assertEqual(self.job.stall_warning, self.WARNING)
        self.assertEqual(
            [
                call.args[0]
                for call in self.log.training.warning.call_args_list
                if "has stalled" in call.args[0]
            ],
            [f"Training job {self.job.identifier} has stalled: {self.WARNING}"],
        )

    def test_a_round_seen_in_the_local_containers(self):
        # node-A and node-B finish round 4, for which node-C had sent its result.
        response, _peer_get = self._poll(
            scraped=[{"job_id": JOB, "rounds_finished": 4, "terminal_status": None}]
        )
        self._write_job_log(
            *self.BEFORE_THE_ROUND_ENDS,
            _status("2026-11-18 01:04:00,000", "node-A", 4),
            _status("2026-11-18 01:04:05,000", "node-B", 4),
        )

        self._assert_the_warning_stays_and_is_logged_once(response)

    def test_a_round_reported_by_another_hub(self):
        # node-B's Hub has recorded its own finished round 4.
        SwarmParticipant.objects.create(
            network=self.network,
            user=self.user,
            role="CLIENT",
            participant_id="node-B",
            ip="100.64.0.10",
        )
        self.network.gossip_token = "t" * 64
        self.network.save(update_fields=["gossip_token"])

        response, peer_get = self._poll(
            peer_job={
                "flare_job_id": JOB,
                "status": "RUNNING",
                "total_rounds": 10,
                "rounds_finished": 4,
                "progress_percent": 50,
            }
        )

        peer_get.assert_called_once()
        self._assert_the_warning_stays_and_is_logged_once(response)


class FirstRoundProgressTests(TestCase):
    """The progress shown agrees with the monitor before round 0 has finished."""

    def setUp(self):
        user = User.objects.create_user(
            username="first-round", password="test-password"
        )  # nosec B106
        self.project = Project.objects.create(title="First Round Project", author=user)
        self.network = SwarmNetwork.objects.create(
            name="First Round Network",
            project=self.project,
            author=user,
            status="RUNNING",
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _progress(self, rounds_finished):
        job = TrainingJob.objects.create(
            project=self.project,
            network=self.network,
            status="RUNNING",
            flare_job_id=JOB,
            flare_job_uuid=JOB,
            total_rounds=10,
            rounds_finished=rounds_finished,
        )
        with override_settings(
            BASE_DIR=self.tmp.name,
            CACHES={
                "default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}
            },
        ), patch(
            "training.runtime.summarize_runtime_container_logs", return_value=None
        ):
            return get_training_progress_info(job, self.network)

    def test_no_finished_round_is_0_percent_without_an_eta(self):
        info = self._progress(None)

        self.assertEqual(info["progress"], 0)
        self.assertEqual(info["eta"], "Calculating...")

    def test_round_0_finished_is_the_first_of_ten(self):
        self.assertEqual(self._progress(0)["progress"], 10)


class StallWarningCLITests(TestCase):
    """swarmed training status, list and watch show the warning."""

    WARNING = (
        "Site node-A dropped out at 2026-11-17 10:47 UTC: the FL server reports its "
        "training job as missing. The other sites are waiting; check the site, then "
        "stop this job and resume it (swarmed training resume)."
    )

    def setUp(self):
        self.user = User.objects.create_user(
            username="cli-stall", password="test-password"
        )  # nosec B106
        project = Project.objects.create(title="CLI Stall Project", author=self.user)
        network = SwarmNetwork.objects.create(
            name="CLI Stall Network",
            project=project,
            author=self.user,
            status="RUNNING",
        )
        self.job = TrainingJob.objects.create(
            project=project,
            network=network,
            status="RUNNING",
            flare_job_id=JOB,
            flare_job_uuid=JOB,
            last_round_at=timezone.now(),
            stall_warning=self.WARNING,
        )
        payload = {
            "status": "Running",
            "progress": 40,
            "duration": "1h",
            "eta": "1h",
            "job_id": JOB,
        }
        for target, value in (
            (
                "training.services.build_training_status_payload",
                MagicMock(return_value=payload),
            ),
            (
                "training.services.get_training_progress_info",
                MagicMock(return_value=RUNNING_INFO),
            ),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, *argv):
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = cli_main([*argv, "--user", self.user.username])
        return code, stdout.getvalue()

    def test_status_prints_a_warning_line(self):
        code, output = self._run("training", "status", str(self.job.identifier))

        self.assertEqual(code, 0)
        self.assertIn(f"\nwarning: {self.WARNING}\n", output)

    def test_status_json_carries_the_warning(self):
        code, output = self._run(
            "training", "status", str(self.job.identifier), "--json"
        )
        envelope = json.loads(output)

        self.assertEqual(code, 0)
        self.assertEqual(envelope["result"]["stall_warning"], self.WARNING)
        self.assertEqual(
            envelope["result"]["last_round_at"], self.job.last_round_at.isoformat()
        )
        self.assertEqual(envelope["warnings"], [self.WARNING])

    def test_list_names_the_stalled_job(self):
        code, output = self._run(
            "training", "list", "--network", str(self.job.network.identifier), "--json"
        )
        envelope = json.loads(output)

        self.assertEqual(code, 0)
        self.assertEqual(envelope["result"]["jobs"][0]["stall_warning"], self.WARNING)
        self.assertEqual(
            envelope["warnings"], [f"Job {self.job.identifier}: {self.WARNING}"]
        )

    def test_watch_prints_the_warning_once_it_appears(self):
        TrainingJob.objects.filter(pk=self.job.pk).update(stall_warning="")
        calls = []

        def monitor_flags_the_job(*_args, **_kwargs):
            calls.append(None)
            if len(calls) == 2:
                TrainingJob.objects.filter(pk=self.job.pk).update(
                    stall_warning=self.WARNING
                )

        with patch("time.sleep", side_effect=monitor_flags_the_job):
            code, output = self._run(
                "training",
                "watch",
                str(self.job.identifier),
                "--interval",
                "1",
                "--timeout",
                "3",
            )

        self.assertEqual(code, 0)
        # Printed when it appears, and again with the result.
        self.assertEqual(output.count(f"warning: {self.WARNING}"), 2)
        self.assertLess(output.index("Running 40%"), output.index("warning: "))


class StallWarningAdminTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(
            username="admin-stall", password="test-password"
        )  # nosec B106
        project = Project.objects.create(title="Admin Stall Project", author=user)
        network = SwarmNetwork.objects.create(
            name="Admin Stall Network", project=project, author=user, status="RUNNING"
        )
        self.job = TrainingJob.objects.create(
            project=project, network=network, status="RUNNING", stall_warning="stalled"
        )
        self.model_admin = TrainingJobAdmin(TrainingJob, admin.site)

    def test_stalled_column(self):
        self.assertTrue(self.model_admin.stalled(self.job))
        self.job.stall_warning = ""
        self.assertFalse(self.model_admin.stalled(self.job))

    def test_stopping_a_job_clears_its_warning(self):
        request = RequestFactory().post("/")
        with patch.object(self.model_admin, "message_user"):
            self.model_admin.stop_selected_jobs(request, TrainingJob.objects.all())
        self.job.refresh_from_db()

        self.assertEqual(self.job.status, "STOPPED")
        self.assertEqual(self.job.stall_warning, "")


class StallWarningPageTests(TestCase):
    """The Training page and the dashboard show the warning of a running job."""

    WARNING = StallWarningCLITests.WARNING

    def setUp(self):
        environ = patch.dict(os.environ, {"SWARMMEDHUB_DISABLE_ASYNC_LOGGING": "1"})
        environ.start()
        self.addCleanup(environ.stop)
        user = User.objects.create_user(
            username="page-stall", password="test-password"
        )  # nosec B106
        user.profile.accepted_terms = True
        user.profile.accepted_policy = True
        user.profile.save(update_fields=["accepted_terms", "accepted_policy"])
        project = Project.objects.create(title="Page Stall Project", author=user)
        UserCurrentProject.objects.create(user=user, project=project)
        network = SwarmNetwork.objects.create(
            name="Page Stall Network", project=project, author=user, status="RUNNING"
        )
        self.job = TrainingJob.objects.create(
            project=project,
            network=network,
            status="RUNNING",
            flare_job_id=JOB,
            flare_job_uuid=JOB,
            stall_warning=self.WARNING,
        )
        self.client.force_login(user)

    def _training_page(self):
        with patch(
            "training.views.get_training_progress_info", return_value=RUNNING_INFO
        ):
            return self.client.get(reverse("training:training"))

    def test_training_page_shows_the_warning_under_the_progress_bar(self):
        response = self._training_page()

        self.assertContains(response, 'id="stallWarning" role="alert" class="flex ')
        self.assertContains(response, self.WARNING)

    def test_training_page_hides_the_warning_box_without_a_warning(self):
        TrainingJob.objects.filter(pk=self.job.pk).update(stall_warning="")

        response = self._training_page()

        self.assertContains(
            response, 'id="stallWarning" role="alert" class="hidden flex '
        )

    def test_dashboard_shows_a_badge_with_the_warning_as_tooltip(self):
        with patch("home.views.get_storage_stats", return_value=(0, 0, 0)):
            response = self.client.get(reverse("home:dashboard"))

        self.assertContains(
            response,
            f'id="dashboardStallWarning" title="{self.WARNING}" class="inline-flex ',
        )
