"""Regression tests for the polling stall that paged with no log trail.

2026-08-15 20:18: Kuma saw /health return 503, and it recovered at 20:26 on its
own. No error was logged and chat never dropped, so it was the Helix polling
loop. The fetch had no timeout of its own (aiohttp's default is 5 minutes), so
a call that hung and then succeeded made poll_age go stale without a single
failure to log. These tests pin down: a hang becomes a logged failure, a slow
success leaves a WARNING, and going stale / recovering is logged once each.

Stdlib only:

    python3 -m unittest test_polling_stall -v
"""
import asyncio
import time
import unittest
from unittest.mock import patch

import twitch_polling
from twitch_polling import Poller


def _poller(**kw) -> Poller:
    p = Poller(config=None, helix=None, webhook=None, state={},
               save_state=lambda s: None, interval_seconds=90, **kw)
    p.id_map = {"1": {"login": "reburve"}}
    return p


class TestPollTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_hung_fetch_is_a_logged_failure(self):
        p = _poller()

        async def hang():
            await asyncio.Event().wait()

        p._fetch_live_streams = hang
        with patch.object(twitch_polling, "POLL_TIMEOUT_SECONDS", 0.05), \
                self.assertLogs("twitch.polling", level="ERROR") as cm:
            await p._poll_once()
        self.assertIn("timed out", cm.output[0])
        self.assertEqual(p._consecutive_failures, 1)
        self.assertNotIn("last_poll_at", p.state)
        self.assertIsNone(p._poll_started)

    async def test_slow_success_leaves_a_warning(self):
        p = _poller()

        async def slow():
            await asyncio.sleep(0.05)
            return []

        async def handle(streams):
            pass

        p._fetch_live_streams = slow
        p._handle_streams = handle
        with patch.object(twitch_polling, "SLOW_POLL_SECONDS", 0.01), \
                self.assertLogs("twitch.polling", level="WARNING") as cm:
            await p._poll_once()
        self.assertIn("slow", cm.output[0])
        self.assertIn("last_poll_at", p.state, "A slow poll is still a good poll")
        self.assertEqual(p._consecutive_failures, 0)


class TestStallLog(unittest.TestCase):
    def test_quiet_while_fresh(self):
        p = _poller()
        p.state["last_poll_at"] = time.time() - 10
        with self.assertNoLogs("twitch.polling"):
            p._check_stall()

    def test_quiet_before_first_poll(self):
        with self.assertNoLogs("twitch.polling"):
            _poller()._check_stall()

    def test_stale_logs_once_with_in_flight_detail(self):
        p = _poller()
        p.state["last_poll_at"] = time.time() - 320
        p._poll_started = time.monotonic() - 230
        with self.assertLogs("twitch.polling", level="WARNING") as cm:
            p._check_stall()
        self.assertIn("Polling stale", cm.output[0])
        self.assertIn("in flight 230s", cm.output[0])
        with self.assertNoLogs("twitch.polling"):
            p._check_stall()  # still stale: no repeat every 30s

    def test_stale_between_polls_names_the_backoff(self):
        p = _poller()
        p.state["last_poll_at"] = time.time() - 400
        p._consecutive_failures = 2
        with self.assertLogs("twitch.polling", level="WARNING") as cm:
            p._check_stall()
        self.assertIn("no poll in flight", cm.output[0])
        self.assertIn("2 consecutive failures", cm.output[0])
        self.assertIn("next sleep 360s", cm.output[0])

    def test_recovery_is_logged_once(self):
        p = _poller()
        p.state["last_poll_at"] = time.time() - 320
        with self.assertLogs("twitch.polling", level="WARNING"):
            p._check_stall()
        p.state["last_poll_at"] = time.time()
        with self.assertLogs("twitch.polling", level="INFO") as cm:
            p._check_stall()
        self.assertIn("recovered", cm.output[0])
        with self.assertNoLogs("twitch.polling"):
            p._check_stall()

    def test_threshold_follows_health(self):
        p = _poller(stale_seconds=600)
        p.state["last_poll_at"] = time.time() - 320
        with self.assertNoLogs("twitch.polling"):
            p._check_stall()


if __name__ == "__main__":
    unittest.main()
