"""Regression tests for the django-axes brute-force lockout settings.

django-axes 6 reads ``AXES_LOCKOUT_PARAMETERS``. The settings defined
``AXES_LOCK_OUT_PARAMETERS`` instead, which axes ignores, so it locked out by
IP address alone. It also took that address from ``REMOTE_ADDR``, which behind
the bundled nginx is nginx's own address for every client, so five failed
logins by anyone locked every user out for the cool-off time.
"""

import os
from unittest.mock import patch

from axes.conf import settings
from axes.helpers import get_client_ip_address, get_client_parameters
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

# nginx's address on the Docker network: the REMOTE_ADDR of every proxied request.
NGINX_ADDR = "172.18.0.5"
ALICE_ADDR = "203.0.113.7"
ATTACKER_ADDR = "198.51.100.66"


def via_nginx(client_addr, sent_forwarded_for=None, **headers):
    """Build a login request as the bundled nginx passes it on to gunicorn.

    nginx sets X-Forwarded-For to ``$proxy_add_x_forwarded_for``: the header the
    client sent, if any, followed by the address nginx itself saw.
    """
    forwarded_for = client_addr
    if sent_forwarded_for:
        forwarded_for = f"{sent_forwarded_for}, {client_addr}"
    return RequestFactory().post(
        reverse("users:two_factor:login"),
        REMOTE_ADDR=NGINX_ADDR,
        HTTP_X_FORWARDED_FOR=forwarded_for,
        **headers,
    )


class LockoutParameterTests(SimpleTestCase):
    """Axes counts failures per username and client address combination."""

    def test_axes_reads_the_username_and_address_combination(self):
        # The misspelt setting left axes on its fallback, ["ip_address"].
        self.assertEqual(
            settings.AXES_LOCKOUT_PARAMETERS, [["username", "ip_address"]]
        )
        self.assertEqual(
            get_client_parameters(
                "alice", ALICE_ADDR, "<unknown>", via_nginx(ALICE_ADDR)
            ),
            [{"username": "alice", "ip_address": ALICE_ADDR}],
        )


class ClientAddressTests(SimpleTestCase):
    """Axes takes the client address from the entry nginx appended."""

    def test_address_appended_by_nginx_is_used(self):
        for client_addr in (ALICE_ADDR, "2001:db8::7"):
            with self.subTest(client_addr=client_addr):
                self.assertEqual(
                    get_client_ip_address(via_nginx(client_addr)), client_addr
                )

    def test_headers_sent_by_the_client_are_ignored(self):
        # The leading X-Forwarded-For entries come from the client. app.conf
        # overwrites X-Real-IP, but production.conf passes the client's own
        # header through, so it cannot be trusted either.
        request = via_nginx(
            ATTACKER_ADDR,
            sent_forwarded_for="192.0.2.1, 10.0.0.1",
            HTTP_X_REAL_IP="192.0.2.1",
        )

        self.assertEqual(get_client_ip_address(request), ATTACKER_ADDR)

    def test_request_that_bypassed_nginx_uses_remote_addr(self):
        # For example runserver in development.
        request = RequestFactory().post("/", REMOTE_ADDR=ALICE_ADDR)

        self.assertEqual(get_client_ip_address(request), ALICE_ADDR)

    def test_malformed_forwarded_entry_falls_back_to_remote_addr(self):
        # The address is stored in an inet column, so it has to parse.
        request = RequestFactory().post(
            "/", REMOTE_ADDR=ALICE_ADDR, HTTP_X_FORWARDED_FOR="unknown"
        )

        self.assertEqual(get_client_ip_address(request), ALICE_ADDR)


# A cheap hasher keeps the repeated password checks fast.
@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)
class LockoutThroughNginxTests(TestCase):
    """Failed logins lock out one username from one client address only."""

    def setUp(self):
        # Audit logging would otherwise enqueue a Celery task on every login.
        environ = patch.dict(
            os.environ, {"SWARMMEDHUB_DISABLE_ASYNC_LOGGING": "1"}
        )
        environ.start()
        self.addCleanup(environ.stop)

        self.alice = User.objects.create_user(
            username="alice", password="alice-password"
        )  # nosec B106
        self.bob = User.objects.create_user(
            username="bob", password="bob-password"
        )  # nosec B106

    def test_failures_do_not_lock_out_other_users_or_addresses(self):
        # A new forged X-Forwarded-For prefix on each guess must not reset the
        # count for the attacker's real address.
        for attempt in range(settings.AXES_FAILURE_LIMIT):
            request = via_nginx(
                ATTACKER_ADDR, sent_forwarded_for=f"192.0.2.{attempt + 1}"
            )
            self.assertIsNone(
                authenticate(request, username="alice", password="guess")
            )

        self.assertIsNone(
            authenticate(
                via_nginx(ATTACKER_ADDR),
                username="alice",
                password="alice-password",
            )
        )
        self.assertEqual(
            authenticate(
                via_nginx(ALICE_ADDR), username="alice", password="alice-password"
            ),
            self.alice,
        )
        self.assertEqual(
            authenticate(
                via_nginx(ATTACKER_ADDR), username="bob", password="bob-password"
            ),
            self.bob,
        )
