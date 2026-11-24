"""The status a Hub sends to the other Hubs of a network every 30 seconds."""

from __future__ import annotations

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from .test_failover import NetworkFixture
from .views import broadcast_network_status


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "gossip-broadcast-tests",
        }
    }
)
class UnrecognisedHubNoticeTests(NetworkFixture, TestCase):
    """A Hub none of whose addresses is a participant's sends no status."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_a_hub_that_cannot_tell_which_participant_it_is_says_so_once(self):
        # As a Hub set up with SWARMMEDHUB_LOCAL_PARTICIPANT only, every 30 s.
        with (
            patch("network.views.logger") as log,
            patch("network.views.get_local_addresses", return_value=frozenset()),
            patch("network.tasks.shout_to_peer_task.delay") as shout,
        ):
            for _ in range(3):
                broadcast_network_status(self.network.identifier)

        log.network.warning.assert_not_called()
        log.network.info.assert_called_once()
        notice = log.network.info.call_args.args[0]
        self.assertIn("Status exchange between Hubs is optional", notice)
        self.assertEqual(
            [call.args[0] for call in log.network.debug.call_args_list],
            [notice, notice],
        )
        shout.assert_not_called()
