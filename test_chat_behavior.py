"""Regression tests for two chat behaviours that failed quietly.

1. Unknown commands logged a full traceback. coda-pipeline shares this chat and
   owns `!ads`, so every use produced an exception in sig's log -- noise whose
   real cost is burying an actual command failure.

2. Auto-shout consulted only `state["live_now"]`, which the polling loop fills.
   A stream stop/start empties it, so the first messages after a restart looked
   offline when they were not. That cost CoslinStar their shoutout on 2026-08-01.

Stdlib only -- no new dependency.

    python3 -m unittest test_chat_behavior -v
"""
import asyncio
import unittest

from twitchio.ext import commands

from twitch_chat import TwitchChat, _Bot


class _Log:
    def __init__(self):
        self.errors, self.infos = [], []

    def error(self, msg, *a, **k):
        self.errors.append(str(msg) % a if a else str(msg))

    def exception(self, msg, *a, **k):
        self.errors.append(str(msg) % a if a else str(msg))

    def info(self, msg, *a, **k):
        self.infos.append(str(msg) % a if a else str(msg))

    warning = debug = info


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class UnknownCommands(unittest.TestCase):
    def _bot(self):
        b = _Bot.__new__(_Bot)
        b.log = _Log()
        return b

    def test_command_not_found_is_silent(self):
        b = self._bot()
        run(b.event_command_error(None, commands.CommandNotFound("no such command", "ads")))
        self.assertEqual(b.log.errors, [], "!ads must not log an error")

    def test_other_command_errors_still_logged(self):
        b = self._bot()
        run(b.event_command_error(None, RuntimeError("helix exploded")))
        self.assertTrue(any("helix exploded" in e for e in b.log.errors),
                        "a real command failure must still be logged")


class AutoShout(unittest.TestCase):
    def _chat(self, live_now, stream, cfg_enabled=True):
        c = TwitchChat.__new__(TwitchChat)
        c.log = _Log()
        c._channel = "reburve"
        c.state = {"live_now": live_now}
        c.config = type("C", (), {"raw": {"twitch_chat": {"auto_shout": {
            "enabled": cfg_enabled, "channels": ["coslinstar"]}}}})()
        c.shouted = []

        async def fake_shout(u):
            c.shouted.append(u)

        async def fake_stream():
            if isinstance(stream, Exception):
                raise stream
            return stream

        c._cmd_shoutout = fake_shout
        c._own_stream = fake_stream
        return c

    def test_shouts_when_live_now_is_correct(self):
        c = self._chat(["reburve"], None)
        run(c._maybe_auto_shout("coslinstar"))
        self.assertEqual(c.shouted, ["coslinstar"])

    def test_stale_live_now_falls_back_to_helix(self):
        """The 2026-08-01 case: actually live, but live_now not yet repopulated."""
        c = self._chat([], {"id": "123", "game_name": "Outer Wilds"})
        run(c._maybe_auto_shout("coslinstar"))
        self.assertEqual(c.shouted, ["coslinstar"], "should shout once Helix confirms live")

    def test_genuinely_offline_does_not_shout(self):
        c = self._chat([], None)
        run(c._maybe_auto_shout("coslinstar"))
        self.assertEqual(c.shouted, [], "must not shout into an offline channel")

    def test_helix_failure_fails_closed(self):
        c = self._chat([], RuntimeError("helix 500"))
        run(c._maybe_auto_shout("coslinstar"))
        self.assertEqual(c.shouted, [], "an API error must not produce a shoutout")
        self.assertTrue(c.log.errors, "and it must be logged, not swallowed")

    def test_user_not_on_the_list_is_ignored(self):
        c = self._chat(["reburve"], None)
        run(c._maybe_auto_shout("randomviewer"))
        self.assertEqual(c.shouted, [])

    def test_disabled_config_short_circuits(self):
        c = self._chat(["reburve"], None, cfg_enabled=False)
        run(c._maybe_auto_shout("coslinstar"))
        self.assertEqual(c.shouted, [])


if __name__ == "__main__":
    unittest.main()
