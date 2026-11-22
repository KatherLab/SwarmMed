"""Migration regression tests for networks.

Migration 0004 adds the startup package's fingerprint and SHA-256.
"""

from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from project.models import Project


class PackageFingerprintMigrationTests(TransactionTestCase):
    """Migration 0004 adds package_fingerprint and package_sha256, and can be reversed."""

    migrate_from = [("network", "0003_alter_swarmnetwork_options_and_more")]
    migrate_to = [("network", "0004_swarmnetwork_package_fingerprint")]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps
        OldSwarmNetwork = old_apps.get_model("network", "SwarmNetwork")

        user = User.objects.create(username="fingerprint-migration-user")
        project = Project.objects.create(title="Fingerprint Migration Project", author=user)
        self.network_id = OldSwarmNetwork.objects.create(
            name="Fingerprint Migration Network",
            project_id=project.id,
            author_id=user.id,
            status="PROVISIONED",
            creation_method="IMPORTED",
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
                    cursor, "network_swarmnetwork"
                )
            }

    def test_existing_networks_get_no_fingerprint(self):
        network = self.apps.get_model("network", "SwarmNetwork").objects.get(
            id=self.network_id
        )

        self.assertEqual(network.package_fingerprint, "")
        self.assertEqual(network.package_sha256, "")
        self.assertEqual(network.creation_method, "IMPORTED")

    def test_code_without_the_new_fields_can_still_create_networks(self):
        # As release 1.0.4 does, or a worker that still runs it during an update.
        OldSwarmNetwork = (
            MigrationExecutor(connection)
            .loader.project_state(self.migrate_from)
            .apps.get_model("network", "SwarmNetwork")
        )
        existing = OldSwarmNetwork.objects.get(id=self.network_id)

        created_id = OldSwarmNetwork.objects.create(
            name="Created by 1.0.4",
            project_id=existing.project_id,
            author_id=existing.author_id,
        ).id

        network = self.apps.get_model("network", "SwarmNetwork").objects.get(
            id=created_id
        )
        self.assertEqual(network.package_fingerprint, "")
        self.assertEqual(network.package_sha256, "")

    def test_migration_can_be_reversed(self):
        self.assertTrue({"package_fingerprint", "package_sha256"} <= self._columns())

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)

        self.assertFalse({"package_fingerprint", "package_sha256"} & self._columns())
        old_apps = executor.loader.project_state(self.migrate_from).apps
        self.assertTrue(
            old_apps.get_model("network", "SwarmNetwork")
            .objects.filter(id=self.network_id)
            .exists()
        )


class NetworkMigrationsMatchModelsTests(TestCase):
    def test_no_model_change_lacks_a_migration(self):
        # Exits with status 1 if the models differ from the migrations.
        call_command(
            "makemigrations", "network", "--check", "--dry-run", stdout=StringIO()
        )
