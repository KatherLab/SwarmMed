"""Migration regression tests for training jobs.

Migration 0002 adds the canonical FLARE UUID; 0003 adds the stall warning.
"""

from django.contrib.auth.models import User
from django.db import connection
from django.db import transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from network.models import SwarmNetwork
from project.models import Project


class TrainingJobMigrationTests(TransactionTestCase):
    """Verify backfill and duplicate-job merging during migration 0002."""

    migrate_from = [("results", "0001_initial"), ("training", "0001_initial")]
    migrate_to = [
        ("results", "0001_initial"),
        ("training", "0002_trainingjob_flare_job_uuid"),
    ]

    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        old_apps = self.executor.loader.project_state(self.migrate_from).apps
        self._set_up_before_migration(old_apps)
        transaction.commit()

        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        self.apps = self.executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        # Migrating to results 0001 also unapplied the later results migrations;
        # apply every app's latest migration again for the tests that follow.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _set_up_before_migration(self, apps):
        TrainingJob = apps.get_model("training", "TrainingJob")
        TrainingResult = apps.get_model("results", "TrainingResult")
        ResultsVisualizationRun = apps.get_model(
            "results", "ResultsVisualizationRun"
        )

        self.user = User.objects.create(username="migration-user")
        self.project = Project.objects.create(
            title="Migration Project",
            author=self.user,
        )
        self.network = SwarmNetwork.objects.create(
            name="Migration Network",
            project=self.project,
            author=self.user,
            status="RUNNING",
            creation_method="CREATED",
        )
        self.flare_uuid = "99999999-9999-9999-9999-999999999999"

        self.original_job = TrainingJob.objects.create(
            project_id=self.project.id,
            network_id=self.network.id,
            status="RUNNING",
            flare_job_id=f"Submitted job: {self.flare_uuid}",
            rounds_finished=1,
            progress_percent=20,
        )
        self.duplicate_job = TrainingJob.objects.create(
            project_id=self.project.id,
            network_id=self.network.id,
            status="COMPLETED",
            flare_job_id=self.flare_uuid,
            total_rounds=5,
            rounds_finished=4,
            progress_percent=99,
            completed_at=timezone.now(),
        )

        TrainingResult.objects.create(
            job_id=self.original_job.id,
            file_path=f"{self.project.identifier}/results/{self.flare_uuid}/site-a/model.npy",
            file_size=12,
        )
        TrainingResult.objects.create(
            job_id=self.duplicate_job.id,
            file_path=f"{self.project.identifier}/results/{self.flare_uuid}/site-a/model.npy",
            file_size=34,
        )
        ResultsVisualizationRun.objects.create(
            project_id=self.project.id,
            job_id=self.duplicate_job.id,
            flare_job_id=self.flare_uuid,
            user_id=self.user.id,
            status="completed",
        )

    def test_migration_backfills_and_deduplicates_training_jobs(self):
        TrainingJob = self.apps.get_model("training", "TrainingJob")
        TrainingResult = self.apps.get_model("results", "TrainingResult")
        ResultsVisualizationRun = self.apps.get_model(
            "results", "ResultsVisualizationRun"
        )

        jobs = list(
            TrainingJob.objects.filter(network_id=self.network.id).order_by("created_at")
        )

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.flare_job_uuid, self.flare_uuid)
        self.assertEqual(job.status, "COMPLETED")
        self.assertEqual(job.progress_percent, 100)
        self.assertEqual(job.rounds_finished, 4)

        results = list(TrainingResult.objects.filter(job=job))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].file_size, 34)

        visualization_run = ResultsVisualizationRun.objects.get()
        self.assertEqual(visualization_run.job_id, job.id)


class StallWarningMigrationTests(TransactionTestCase):
    """Migration 0003 adds last_round_at and stall_warning, and can be reversed."""

    migrate_from = [("training", "0002_trainingjob_flare_job_uuid")]
    migrate_to = [("training", "0003_trainingjob_stall_warning")]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        OldTrainingJob = old_apps.get_model("training", "TrainingJob")

        user = User.objects.create(username="stall-migration-user")
        project = Project.objects.create(title="Stall Migration Project", author=user)
        network = SwarmNetwork.objects.create(
            name="Stall Migration Network",
            project=project,
            author=user,
            status="RUNNING",
            creation_method="CREATED",
        )
        self.job_id = OldTrainingJob.objects.create(
            project_id=project.id,
            network_id=network.id,
            status="RUNNING",
            flare_job_id="77777777-7777-7777-7777-777777777777",
            rounds_finished=3,
        ).id
        transaction.commit()

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def _columns(self):
        with connection.cursor() as cursor:
            return {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, "training_trainingjob"
                )
            }

    def test_existing_jobs_get_no_warning_and_no_round_time(self):
        job = self.apps.get_model("training", "TrainingJob").objects.get(
            id=self.job_id
        )

        self.assertEqual(job.stall_warning, "")
        self.assertIsNone(job.last_round_at)
        self.assertEqual(job.rounds_finished, 3)

    def test_migration_can_be_reversed(self):
        self.assertTrue({"last_round_at", "stall_warning"} <= self._columns())

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)

        self.assertFalse({"last_round_at", "stall_warning"} & self._columns())
        old_apps = executor.loader.project_state(self.migrate_from).apps
        self.assertTrue(
            old_apps.get_model("training", "TrainingJob")
            .objects.filter(id=self.job_id)
            .exists()
        )
