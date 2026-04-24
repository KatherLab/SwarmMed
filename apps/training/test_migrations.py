"""Migration regression tests for training job canonical FLARE UUID handling."""

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
