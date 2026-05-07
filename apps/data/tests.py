"""Regression tests for the data app, focused on Hub/CLI parity around
visualization plot lookup and serialization.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import TestCase, override_settings
from django.urls import reverse

from project.models import Project, UserCurrentProject

from .models import VisualizationPlot, VisualizationRun
from .services import resolve_visualization_plot, serialize_visualization_run


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
)
class VisualizationPlotResolutionTests(TestCase):
    """The visualization status JSON returns plot UUID identifiers; the plot
    proxy view must accept those UUIDs and also tolerate legacy numeric ids.
    Both Hub and CLI consumers go through ``resolve_visualization_plot`` so
    behaviour stays identical.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="data-user", password="test-password"
        )  # nosec B106
        self.user.profile.accepted_terms = True
        self.user.profile.accepted_policy = True
        self.user.profile.save()
        self.project = Project.objects.create(
            title="Data project", author=self.user
        )
        self.other_project = Project.objects.create(
            title="Other project", author=self.user
        )
        UserCurrentProject.objects.create(
            user=self.user, project=self.project
        )
        fake_log = SimpleNamespace(
            auth=SimpleNamespace(info=lambda *_args, **_kwargs: None)
        )
        with patch("users.signals.get_logger", return_value=fake_log):
            self.client.force_login(self.user)
        self.run = VisualizationRun.objects.create(
            project=self.project, user=self.user, status="completed"
        )
        self.other_run = VisualizationRun.objects.create(
            project=self.other_project, user=self.user, status="completed"
        )
        self.plot = VisualizationPlot.objects.create(
            visualization_run=self.run,
            title="ROC",
            plot_number=1,
            image_data="dummy.png",
        )
        self.other_plot = VisualizationPlot.objects.create(
            visualization_run=self.other_run,
            title="Other",
            plot_number=1,
            image_data="other.png",
        )

    def test_resolve_by_uuid_identifier(self):
        resolved = resolve_visualization_plot(
            self.project, str(self.plot.identifier)
        )
        self.assertEqual(resolved, self.plot)

    def test_resolve_by_legacy_numeric_id(self):
        resolved = resolve_visualization_plot(self.project, str(self.plot.id))
        self.assertEqual(resolved, self.plot)

    def test_resolve_is_scoped_to_project(self):
        # The other-project plot must not leak even with a valid UUID.
        self.assertIsNone(
            resolve_visualization_plot(
                self.project, str(self.other_plot.identifier)
            )
        )
        # Or by its numeric id either.
        self.assertIsNone(
            resolve_visualization_plot(self.project, str(self.other_plot.id))
        )

    def test_resolve_unknown_id_returns_none(self):
        self.assertIsNone(resolve_visualization_plot(self.project, ""))
        self.assertIsNone(
            resolve_visualization_plot(self.project, "not-a-uuid-or-int")
        )
        self.assertIsNone(resolve_visualization_plot(self.project, "999999"))

    def test_serialize_visualization_run_emits_uuid_identifier(self):
        # The serialized payload feeds the visualization_status JSON which
        # builds plot URLs. The identifier MUST be the UUID so the proxy
        # view's resolver finds the row.
        serialized = serialize_visualization_run(self.run, self.project)
        self.assertEqual(len(serialized["plots"]), 1)
        self.assertEqual(
            serialized["plots"][0]["identifier"], str(self.plot.identifier)
        )

    def test_status_json_plot_url_fetches_by_uuid(self):
        status_response = self.client.get(reverse("data:visualization_status"))
        self.assertEqual(status_response.status_code, 200)
        plot_url = status_response.json()["plots"][0]["image_url"]
        self.assertIn(str(self.plot.identifier), plot_url)

        with patch(
            "data.views._proxy_s3_download_file",
            return_value=HttpResponse(b"png", content_type="image/png"),
        ) as proxy:
            plot_response = self.client.get(plot_url)

        self.assertEqual(plot_response.status_code, 200)
        proxy.assert_called_once_with("dummy.png", "plot_1.png", inline=True)

    def test_plot_url_is_scoped_to_active_project(self):
        other_url = reverse(
            "data:get_visualization_plot",
            args=[str(self.other_plot.identifier), "image"],
        )
        with patch("data.views._proxy_s3_download_file") as proxy:
            response = self.client.get(other_url)

        self.assertEqual(response.status_code, 404)
        proxy.assert_not_called()
