"""Personal-data export: explanation runs without their script output."""

from __future__ import annotations

import json

from django.test import Client
from django.urls import reverse

from results.models import ExplanationRun
from results.test_explanations import ExplanationFixture, login


class ExplanationRunExportTests(ExplanationFixture):
    def test_export_leaves_out_run_output_and_error_message(self):
        run = self.import_bundle(user=self.member)
        ExplanationRun.objects.filter(pk=run.pk).update(
            output="case site-1 outcome=TP score=0.93",
            error_message="Traceback: site-2 row 17",
        )
        client = Client()
        login(client, self.member)

        response = client.get(reverse("users:export_data"))

        self.assertEqual(response.status_code, 200)
        exported = json.loads(response.content)["explanation_runs"]
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0]["identifier"], str(run.identifier))
        self.assertEqual(exported[0]["status"], run.status)
        for key in ("project__title", "source", "created_at", "completed_at"):
            self.assertIn(key, exported[0])
        self.assertNotIn("output", exported[0])
        self.assertNotIn("error_message", exported[0])
        content = response.content.decode()
        self.assertNotIn("outcome=TP", content)
        self.assertNotIn("row 17", content)
