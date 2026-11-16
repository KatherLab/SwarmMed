"""Custom error pages for requests rejected before authentication."""

from __future__ import annotations

from django.test import Client, RequestFactory, TestCase, override_settings

from common import views


@override_settings(ALLOWED_HOSTS=["hub.example.org"], DEBUG=False)
class ErrorHandlerTests(TestCase):
    def test_unknown_host_gets_the_400_page(self):
        # DisallowedHost is raised before the authentication middleware sets
        # request.user, and the 400 handler must still render its page.
        client = Client(raise_request_exception=False)
        response = client.get("/", HTTP_HOST="unknown.example.net")
        self.assertEqual(response.status_code, 400)
        self.assertTemplateUsed(response, "errors/400.html")

    def test_handlers_render_without_request_user(self):
        factory = RequestFactory(HTTP_HOST="hub.example.org")
        for handler, status in (
            (views.bad_request_view, 400),
            (views.permission_denied_view, 403),
            (views.page_not_found_view, 404),
        ):
            request = factory.get("/missing/")
            self.assertFalse(hasattr(request, "user"))
            response = handler(request, exception=Exception("x"))
            self.assertEqual(response.status_code, status)
        request = factory.get("/missing/")
        self.assertEqual(views.server_error_view(request).status_code, 500)
