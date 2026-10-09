"""Tests that network actions follow the project that owns the network.

``project_membership_required`` guards every view that takes a network UUID
from its URL (start/stop/delete, startup-kit export, status, training
start/stop). Access must be decided by the network's own project, not by
whichever project the user currently has selected; otherwise any logged-in
user could act on another project's network just by knowing its UUID.
"""

import io
import os
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages.storage.cookie import CookieStorage
from django.http import Http404, HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.urls import resolve, reverse

from network.models import SwarmNetwork

from .models import Project, UserCurrentProject

# Every view that receives a network UUID from its URL, with its HTTP method.
NETWORK_VIEWS = [
    ("network:start_swarm_network", "get"),
    ("network:stop_swarm_network", "get"),
    ("network:delete_swarm_network", "post"),
    ("network:set_current_network", "get"),
    ("network:download_startup_kits", "get"),
    ("network:get_swarm_network_status", "get"),
    ("training:start_training", "get"),
    ("training:stop_training", "get"),
]


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
)
class NetworkProjectAccessTests(TestCase):
    """Alice works in project A and Bob in project B, which owns the network."""

    def setUp(self):
        # Audit logging would otherwise enqueue a Celery task on every save.
        environ = patch.dict(
            os.environ, {"SWARMMEDHUB_DISABLE_ASYNC_LOGGING": "1"}
        )
        environ.start()
        self.addCleanup(environ.stop)

        self.factory = RequestFactory()
        self.alice = User.objects.create_user(
            username="alice", password="test-password"
        )  # nosec B106
        self.bob = User.objects.create_user(
            username="bob", password="test-password"
        )  # nosec B106
        self.project_a = Project.objects.create(
            title="Project A", author=self.alice
        )
        self.project_b = Project.objects.create(
            title="Project B", author=self.bob
        )
        UserCurrentProject.objects.create(
            user=self.alice, project=self.project_a
        )
        UserCurrentProject.objects.create(
            user=self.bob, project=self.project_b
        )
        self.network_b = SwarmNetwork.objects.create(
            name="Network B",
            project=self.project_b,
            author=self.bob,
            status="PROVISIONED",
        )

    def _call(self, user, url_name, method="get"):
        """Runs the view behind ``url_name`` for network B as ``user``, with
        the URL kwargs exactly as the resolver passes them (a UUID object).
        """
        url = reverse(url_name, args=[self.network_b.identifier])
        request = getattr(self.factory, method)(url)
        request.user = user
        request._messages = CookieStorage(request)
        match = resolve(url)
        return match.func(request, *match.args, **match.kwargs)

    @patch("network.services.export_startup_package")
    def test_member_of_other_project_cannot_download_startup_kits(
        self, export_startup_package
    ):
        with self.assertRaises(Http404):
            self._call(self.alice, "network:download_startup_kits")

        export_startup_package.assert_not_called()

    @patch("network.services.start_network")
    def test_member_of_other_project_cannot_start_network(self, start_network):
        with self.assertRaises(Http404):
            self._call(self.alice, "network:start_swarm_network")

        start_network.assert_not_called()

    @patch("training.services.stop_training_job")
    @patch("training.services.submit_training_job")
    @patch("network.services.stop_network")
    @patch("network.services.start_network")
    @patch("network.services.export_startup_package")
    def test_every_network_view_hides_other_projects_network(self, *services):
        for url_name, method in NETWORK_VIEWS:
            with self.subTest(url_name), self.assertRaises(Http404):
                self._call(self.alice, url_name, method)

        for service in services:
            service.assert_not_called()

    @patch(
        "network.services.export_startup_package",
        return_value=io.BytesIO(b"startup-kits"),
    )
    def test_project_member_can_download_startup_kits(
        self, export_startup_package
    ):
        response = self._call(self.bob, "network:download_startup_kits")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response.content, b"startup-kits")
        export_startup_package.assert_called_once_with(self.network_b)

    @patch("network.services.start_network")
    def test_project_member_can_start_network(self, start_network):
        response = self._call(self.bob, "network:start_swarm_network")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("network:network"))
        start_network.assert_called_once_with(self.network_b, self.bob)

    @patch(
        "network.services.export_startup_package",
        return_value=io.BytesIO(b"startup-kits"),
    )
    def test_membership_counts_while_another_project_is_current(
        self, export_startup_package
    ):
        # Alice keeps project A selected; being a member of B is what counts.
        self.project_b.members.add(self.alice)

        response = self._call(self.alice, "network:download_startup_kits")

        self.assertEqual(response.status_code, 200)
        export_startup_package.assert_called_once_with(self.network_b)

    @patch(
        "network.services.export_startup_package",
        return_value=io.BytesIO(b"startup-kits"),
    )
    def test_emergency_access_still_reaches_any_network(
        self, export_startup_package
    ):
        self.alice.profile.is_emergency_access = True
        self.alice.profile.save()

        response = self._call(self.alice, "network:download_startup_kits")

        self.assertEqual(response.status_code, 200)
        export_startup_package.assert_called_once_with(self.network_b)

    @patch("project.decorators.render", return_value=HttpResponse("picker"))
    @patch("network.services.export_startup_package")
    def test_member_without_current_project_still_gets_project_picker(
        self, export_startup_package, render
    ):
        UserCurrentProject.objects.filter(user=self.bob).delete()

        response = self._call(self.bob, "network:download_startup_kits")

        self.assertEqual(response.content, b"picker")
        self.assertEqual(
            render.call_args.args[1], "apps/project/no_project_selected.html"
        )
        export_startup_package.assert_not_called()
