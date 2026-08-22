"""Regression tests for TwitchChat.connected.

These exist because the bug they cover shipped twice and hid for days each time.
`connected` used to return twitchio's `conn.is_alive`, which only reports whether
the websocket object is open. A dead access token still opens a socket, fails IRC
auth, and closes -- so the transport looked healthy while chat was completely
dead. The reconnect watchdog, /health and /say all read this one property, so all
three lied at once and Uptime Kuma stayed green.

Every test below is a failure that actually happened or that the old check
provably could not see. Stdlib only -- no new dependency.

    python3 -m unittest test_chat_liveness -v
"""
import time
import unittest

from twitch_chat import TwitchChat, IRC_SILENCE_SECONDS


class _Conn:
    def __init__(self, is_alive: bool) -> None:
        self.is_alive = is_alive


class _Bot:
    def __init__(self, is_alive: bool = True) -> None:
        self._connection = _Conn(is_alive)


class _Log:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def _record(self, msg, *a, **k):
        self.messages.append(str(msg) % a if a else str(msg))

    warning = info = error = debug = _record


def _chat(joined: bool = True, socket_alive: bool = True,
          silent_for: float = 0.0) -> TwitchChat:
    """A TwitchChat with only the liveness state populated."""
    c = TwitchChat.__new__(TwitchChat)
    c.log = _Log()
    c._bot = _Bot(socket_alive)
    c._channel = "reburve"
    c._joined_at = time.monotonic() if joined else None
    c._last_irc_line = time.monotonic() - silent_for
    return c


class ConnectedProperty(unittest.TestCase):

    def test_healthy_session_is_connected(self):
        self.assertTrue(_chat().connected)

    def test_socket_open_but_never_joined_is_not_connected(self):
        """The 2026-08-17 outage: socket kept reopening, JOIN never succeeded.

        The old check returned True here for four days.
        """
        c = _chat(joined=False, socket_alive=True)
        self.assertFalse(c.connected)

    def test_auth_rejection_mid_session_marks_dead(self):
        """Joined fine, then the access token expired and Twitch rejected us."""
        c = _chat(joined=True)
        self.assertTrue(c.connected)
        c._note_irc_line(":tmi.twitch.tv NOTICE * :Login authentication failed")
        self.assertFalse(c.connected)
        self.assertTrue(any("rejected the chat credentials" in m
                            for m in c.log.messages))

    def test_reconnect_request_clears_ready_until_rejoin(self):
        c = _chat(joined=True)
        c._note_irc_line(":tmi.twitch.tv RECONNECT")
        self.assertFalse(c.connected)

    def test_silent_session_is_not_connected(self):
        """Socket open, joined, but no IRC line in longer than Twitch's PING."""
        c = _chat(silent_for=IRC_SILENCE_SECONDS + 1)
        self.assertFalse(c.connected)

    def test_traffic_just_inside_the_window_is_connected(self):
        c = _chat(silent_for=IRC_SILENCE_SECONDS - 5)
        self.assertTrue(c.connected)

    def test_closed_socket_is_not_connected(self):
        self.assertFalse(_chat(socket_alive=False).connected)

    def test_no_bot_is_not_connected(self):
        c = _chat()
        c._bot = None
        self.assertFalse(c.connected)

    def test_ordinary_traffic_does_not_clear_ready(self):
        """A PRIVMSG mentioning the word 'reconnect' must not trip the RECONNECT
        branch -- that check looks for the IRC command, not the word."""
        c = _chat(joined=True)
        c._note_irc_line(
            ":viewer!viewer@viewer.tmi.twitch.tv PRIVMSG #reburve :did it reconnect yet"
        )
        self.assertTrue(c.connected)

    def test_server_join_line_restores_ready_after_reconnect(self):
        """A routine Twitch RECONNECT must not cost a full rebuild.

        twitchio dispatches `ready` only once per bot object, so the JOIN line
        from the server is the only evidence a rejoin succeeded.
        """
        c = _chat(joined=True)
        c._channel = "reburve"
        c._note_irc_line(":tmi.twitch.tv RECONNECT")
        self.assertFalse(c.connected)
        c._note_irc_line(":sig1852!sig1852@sig1852.tmi.twitch.tv JOIN #reburve")
        self.assertTrue(c.connected)

    def test_join_to_a_different_channel_is_ignored(self):
        c = _chat(joined=False)
        c._channel = "reburve"
        c._note_irc_line(":someone!x@x.tmi.twitch.tv JOIN #someotherchannel")
        self.assertFalse(c.connected)

    def test_any_line_refreshes_the_silence_timer(self):
        c = _chat(silent_for=IRC_SILENCE_SECONDS + 1)
        self.assertFalse(c.connected)
        c._note_irc_line("PING :tmi.twitch.tv")
        self.assertTrue(c.connected)


class DisconnectedSeconds(unittest.TestCase):

    def test_zero_while_connected(self):
        self.assertEqual(_chat().connected and _chat().disconnected_seconds, 0.0)

    def test_grows_once_dead(self):
        c = _chat(joined=False)
        c._last_connected = time.monotonic() - 300
        self.assertGreater(c.disconnected_seconds, 250)


if __name__ == "__main__":
    unittest.main()
