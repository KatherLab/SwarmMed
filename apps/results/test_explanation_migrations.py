"""Migration tests for the explainability tables and the project field."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

EXPLANATION_TABLES = {
    "results_explanationrun",
    "results_explanationcase",
    "results_explanationpanel",
    "results_explanationsummary",
    "results_explanationreview",
    "results_explanationreader",
}


def _tables():
    return set(connection.introspection.table_names())


def _columns(table):
    with connection.cursor() as cursor:
        return {
            column.name
            for column in connection.introspection.get_table_description(cursor, table)
        }


class ExplanationMigrationTests(TransactionTestCase):
    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_migrations_apply_and_reverse(self):
        self.assertTrue(_tables() >= EXPLANATION_TABLES)
        self.assertIn("explainability_script", _columns("project_project"))

        executor = MigrationExecutor(connection)
        executor.migrate(
            [("results", "0001_initial"), ("project", "0002_project_secret")]
        )
        self.assertFalse(EXPLANATION_TABLES & _tables())
        self.assertNotIn("explainability_script", _columns("project_project"))

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(
            [
                ("project", "0003_project_explainability_script"),
                ("results", "0002_explanations"),
            ]
        )
        self.assertTrue(_tables() >= EXPLANATION_TABLES)
        self.assertIn("explainability_script", _columns("project_project"))
