"""Regression tests for two access-control paths that ended in server errors.

``toggle_emergency_access`` raised ``PermissionDenied`` without importing it,
so a non-superuser who got past ``admin_required`` hit a ``NameError`` instead
of a 403. ``GDPRRestrictionMiddleware`` rendered ``errors/restricted.html``,
which did not exist, so every page a restricted user opened failed.
"""

import os
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import (
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import reverse

from users import views


@patch("users.views.get_object_or_404")
@patch("users.views.logger")
class ToggleEmergencyAccessDenialTests(SimpleTestCase):
    """Only superusers may grant or revoke break-glass access."""

    def _toggle_as(self, profile):
        # ``admin_required`` admits admin-role and break-glass users, so the
        # superuser check inside the view is what has to turn them away.
        request = RequestFactory().post(
            reverse("users:toggle_emergency_access", args=[42]),
            {"justification": "test", "duration": "4"},
        )
        request.user = Mock(
            is_authenticated=True,
            is_superuser=False,
            username="mallory",
            profile=profile,
        )
        return views.toggle_emergency_access(request, id=42)

    def test_admin_role_user_is_denied(self, mock_logger, mock_get_object):
        with self.assertRaises(PermissionDenied):
            self._toggle_as(Mock(role="admin", is_emergency_access=False))

        mock_get_object.assert_not_called()
        warning = mock_logger.get_logger.return_value.access.warning
        warning.assert_called_once()
        self.assertIn(
            "UNAUTHORIZED EMERGENCY ACCESS ATTEMPT by mallory",
            warning.call_args.args[0],
        )

    def test_break_glass_user_cannot_toggle_access(
        self, mock_logger, mock_get_object
    ):
        with self.assertRaises(PermissionDenied):
            self._toggle_as(Mock(role="user", is_emergency_access=True))

        mock_get_object.assert_not_called()


# Sessions and the unread-message badge both use the cache, which is Redis in
# the real settings; a local-memory cache keeps these tests self-contained.
@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
)
class GDPRRestrictionPageTests(TestCase):
    """A restricted user is shown the 'Account Restricted' page with a 403."""

    def setUp(self):
        # Audit logging would otherwise enqueue a Celery task on every save.
        environ = patch.dict(
            os.environ, {"SWARMMEDHUB_DISABLE_ASYNC_LOGGING": "1"}
        )
        environ.start()
        self.addCleanup(environ.stop)

        self.user = User.objects.create_user(
            username="restricted", password="test-password"
        )  # nosec B106
        # Accept the legal documents so ``LegalAcceptanceMiddleware`` does not
        # redirect the unrestricted control case before the view runs.
        profile = self.user.profile
        profile.accepted_terms = True
        profile.accepted_policy = True
        profile.is_restricted = True
        profile.save(
            update_fields=["accepted_terms", "accepted_policy", "is_restricted"]
        )
        self.client.force_login(self.user)

    def test_restricted_user_gets_restricted_page(self):
        response = self.client.get(reverse("home:dashboard"))

        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "errors/restricted.html")
        self.assertContains(response, "Account Restricted", status_code=403)

    def test_privacy_policy_and_sign_out_remain_reachable(self):
        # The restricted page points the user to exactly these two routes.
        self.assertEqual(self.client.get(reverse("privacy")).status_code, 200)

        response = self.client.post(reverse("users:signout"))

        self.assertRedirects(
            response,
            reverse("users:two_factor:login"),
            fetch_redirect_response=False,
        )
        self.assertFalse(response.wsgi_request.user.is_authenticated)

    def test_unrestricted_user_is_not_blocked(self):
        self.user.profile.is_restricted = False
        self.user.profile.save(update_fields=["is_restricted"])

        response = self.client.get(reverse("users:index"))

        self.assertEqual(response.status_code, 200)
