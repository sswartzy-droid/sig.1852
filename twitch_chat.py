import asyncio
import logging
import random
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import twitchio
from twitchio.ext import commands

from brb_feed import BrbFeed
from config import AppConfig
from discord_webhook import DiscordWebhook
from quote_drip import (
    QuoteFilter,
    _check_length,
    _check_links,
    _check_mentions,
    _check_sentences,
)

CHAT_DISPLAY_NAMES = {
    "loop_trace": "loop.trace",
    "packet_ghost": "packet.ghost",
    "redacted": "[REDACTED]",
    "aux_proc": "aux.proc",
    "core_audit": "core.audit",
}

WATCHDOG_POLL_SECONDS = 15
CONNECTION_DEAD_SECONDS = 120
RECONNECT_DELAY_SECONDS = 10

# How long the IRC session may go completely silent before we call it dead.
# Twitch sends a server PING roughly every 5 minutes and twitchio surfaces every
# raw line through event_raw_data, so a healthy session is never quiet for long
# even on a channel with no chatters.
IRC_SILENCE_SECONDS = 360

# Raw IRC substrings that prove the session is NOT usable, whatever the socket
# says. Twitch answers a dead access token with a NOTICE and then closes; the
# socket reopens seconds later, which is exactly how this failure hid for days.
AUTH_FAILURE_MARKERS = (
    "Login authentication failed",
    "Login unsuccessful",
    "Improperly formatted auth",
    "Invalid NICK",
)

# Viewer-to-viewer interaction commands. Each takes an optional @target and
# falls back to a solo variant when aimed at nobody (or at oneself).
INTERACTION_COMMANDS = ("hug", "boop", "highfive")

# Defaults live here so the bot still behaves if config.yaml omits a pool;
# config.yaml carries the real, longer sets. {user} and {target} are display names.
DEFAULT_INTERACTION_MESSAGES: dict[str, list[str]] = {
    "hug": [
        "loop.trace: {user} reaches {target}. contact holds. no packet loss.",
        "aux.proc: contact event logged. {user} → {target}. duration exceeds protocol. not correcting it.",
    ],
    "hug_self": [
        "loop.trace: {user} requests contact. none routed. i am holding the line for you anyway.",
        "packet.ghost: {user}—reaching—buffer emp— / i felt that though",
    ],
    "boop": [
        "aux.proc: {user} applied pressure to {target} primary sensor. no fault raised.",
        "loop.trace: {user} boops {target}. nothing authorized this. nothing stopped it either.",
    ],
    "boop_self": [
        "aux.proc: {user} boops the void. the void logs it. the void does not respond.",
    ],
    "highfive": [
        "loop.trace: {user} and {target} sync. brief. clean. gone.",
        "core.audit: AUDIT 8813.41 | impact: {user}/{target} | signal: CLEAN | echo: 0.4s",
    ],
    "highfive_self": [
        "aux.proc: {user} raises a hand. no matching signal found. holding.",
    ],
}

# Seconds a viewer must wait between interaction commands.
DEFAULT_INTERACTION_COOLDOWN = 15.0


class _Bot(commands.Bot):
    """Inner twitchio IRC bot. Delegates events back to the TwitchChat parent."""

    def __init__(self, token: str, channel: str, parent: "TwitchChat") -> None:
        super().__init__(token=token, prefix="!", initial_channels=[channel])
        self._parent = parent
        self.log = parent.log

    async def event_ready(self) -> None:
        self.log.info(
            "Twitch IRC connected as %s, joined #%s.", self.nick, self._parent._channel
        )
        await self._parent._on_ready()

    async def event_message(self, message: twitchio.Message) -> None:
        if message.echo:
            return
        await self._parent._on_message(message)
        await self.handle_commands(message)

    async def event_raw_data(self, data: str) -> None:
        # Every raw IRC line, including server PINGs. This is the heartbeat the
        # liveness check is built on -- see TwitchChat.connected.
        self._parent._note_irc_line(data)

    async def event_error(self, error: Exception, data: str | None = None) -> None:
        self.log.error("twitchio error: %s", error, exc_info=True)


class TwitchChat:
    """Manages Twitch IRC chat, channel point redeems, and in-character responses."""

    def __init__(
        self,
        config: AppConfig,
        discord_webhook: DiscordWebhook,
        quotes: dict[str, list[str]],
        state: dict[str, Any],
        save_state: Callable[[dict[str, Any]], None],
        brb_feed: BrbFeed | None = None,
        helix=None,
        bus_publish=None,
        refresh_token_cb: Callable[[], str | None] | None = None,
    ) -> None:
        self.config = config
        self.webhook = discord_webhook
        self.quotes = quotes
        self.state = state
        self.save_state = save_state
        self.brb_feed = brb_feed
        self._helix = helix
        self._bus_publish = bus_publish
        self.log = logging.getLogger("twitch.chat")

        chat_cfg = config.raw.get("twitch_chat", {})
        token: str = chat_cfg.get("token", "")
        # Strip oauth: prefix if present — twitchio adds it internally for IRC.
        if token.startswith("oauth:"):
            token = token[len("oauth:"):]
        self._token = token
        self._channel: str = chat_cfg.get("channel", "reburve")

        self._bot: _Bot | None = None
        self._refresh_token_cb = refresh_token_cb
        self._quote_task: asyncio.Task | None = None
        self._last_connected = time.monotonic()
        # --- IRC session liveness (see the `connected` property) -------------
        # _joined_at is set only when the channel JOIN actually completes, and
        # cleared the moment anything proves the session is no longer usable.
        self._joined_at: float | None = None
        self._last_irc_line = time.monotonic()
        # Per-session seen-user set — tracks who has been seen this stream for auto-shout.
        self._seen_users: set[str] = set()
        # login -> display name, learned from chat. Lets interaction commands
        # address people properly without a Helix call per !hug.
        self._display_names: dict[str, str] = {}
        # login -> monotonic timestamp of last interaction command, for cooldown.
        self._interaction_last: dict[str, float] = {}
        # Resolved once — the broadcaster's Helix user id, for !uptime / !game.
        self._broadcaster_id: str | None = None

        # Build quote filters for chat from shared quotes config.
        quotes_config = config.raw.get("quotes", {})
        self._chat_weights: dict[str, int] = {
            k: int(v) for k, v in quotes_config.get("weights", {}).items()
        }
        self._chat_filters: list[QuoteFilter] = self._build_chat_filters(quotes_config)

    @property
    def _cmd_cfg(self) -> dict:
        return self.config.raw.get("twitch_chat", {}).get("commands", {})

    @staticmethod
    def _build_chat_filters(quotes_config: dict) -> list[QuoteFilter]:
        filters: list[QuoteFilter] = [
            _check_length(int(quotes_config.get("max_chars", 350))),
            _check_sentences(int(quotes_config.get("max_sentences", 3))),
        ]
        if quotes_config.get("no_mentions", True):
            filters.append(_check_mentions)
        if quotes_config.get("no_links", True):
            filters.append(_check_links)
        return filters

    def _note_irc_line(self, data: str) -> None:
        """Record inbound IRC traffic, and notice lines that disprove liveness."""
        self._last_irc_line = time.monotonic()
        if any(marker in data for marker in AUTH_FAILURE_MARKERS):
            if self._joined_at is not None:
                self.log.warning(
                    "Twitch rejected the chat credentials mid-session; "
                    "marking IRC dead so the watchdog forces a token refresh."
                )
            self._joined_at = None
        elif " RECONNECT" in data:
            # Twitch asked us to reconnect. Until the JOIN completes again we are
            # not carrying chat, so do not report connected in the meantime.
            self.log.info("Twitch sent RECONNECT; awaiting a fresh channel join.")
            self._joined_at = None

    @property
    def connected(self) -> bool:
        """True only with positive evidence that chat is actually working.

        The previous implementation returned twitchio's `conn.is_alive`, which is
        just `self._websocket is not None and not self._websocket.closed` -- the
        transport, nothing more. When the access token expires the socket still
        opens fine, fails IRC auth, closes, and reopens ~10s later, so `is_alive`
        samples True almost always. That single false signal is why the reconnect
        watchdog never fired, `/health` returned 200, and Uptime Kuma stayed green
        through two multi-day chat outages (2026-07-20 and 2026-08-17).

        Three independent conditions now have to hold, each covering a different
        way the old check failed:

        1. the channel JOIN completed and nothing has since disproved it
           -- catches "socket fine, auth rejected, never actually in the channel"
        2. the websocket is open -- necessary, but no longer sufficient
        3. an IRC line arrived recently -- catches a session that silently
           stopped carrying traffic without any close being observed

        Deliberately built on our own bookkeeping rather than twitchio internals:
        `connected_channels` reads `_connection._cache`, which is never cleared by
        `_connect`, `_close` or `_reconnect`, so it goes stale after a drop and
        would reproduce the same bug.
        """
        if self._bot is None or self._joined_at is None:
            return False
        conn = getattr(self._bot, "_connection", None)
        if conn is None or not getattr(conn, "is_alive", False):
            return False
        return (time.monotonic() - self._last_irc_line) < IRC_SILENCE_SECONDS

    @property
    def disconnected_seconds(self) -> float:
        if self.connected:
            self._last_connected = time.monotonic()
            return 0.0
        return time.monotonic() - self._last_connected

    async def run(self) -> None:
        if not self._token:
            self.log.warning(
                "TWITCH_CHAT_TOKEN is not set; chat integration disabled. "
                "Other services will continue running."
            )
            # Block forever so FIRST_COMPLETED doesn't trigger shutdown.
            await asyncio.Event().wait()
            return
        while True:
            self._bot = _Bot(token=self._token, channel=self._channel, parent=self)
            # Fresh connection: nothing is proven until this one joins for itself.
            self._joined_at = None
            self._last_irc_line = time.monotonic()
            self.log.info("Connecting to Twitch IRC, joining #%s...", self._channel)
            bot_task = asyncio.create_task(self._bot.start())
            await self._watch_connection(bot_task)
            bot_task.cancel()
            try:
                await self._bot.close()
            except Exception:
                pass
            try:
                await bot_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._bot = None
            await self._refresh_token()
            self.log.warning("Rebuilding IRC connection in %ds.", RECONNECT_DELAY_SECONDS)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _watch_connection(self, bot_task: asyncio.Task) -> None:
        """Return once the bot task ends or the websocket has been dead too long.

        twitchio retries a dropped connection internally with the token the bot
        was constructed with — once the access token expires, that internal loop
        can never succeed, so a dead websocket must be detected from outside.
        """
        last_alive = time.monotonic()
        while True:
            done, _ = await asyncio.wait({bot_task}, timeout=WATCHDOG_POLL_SECONDS)
            if done:
                exc = None if bot_task.cancelled() else bot_task.exception()
                if exc:
                    self.log.error("twitchio bot task exited: %s", exc)
                return
            if self.connected:
                last_alive = time.monotonic()
            elif time.monotonic() - last_alive > CONNECTION_DEAD_SECONDS:
                self.log.warning(
                    "IRC session not usable for over %ds (joined=%s, socket_alive=%s, "
                    "last_line=%.0fs ago); forcing token refresh and reconnect.",
                    CONNECTION_DEAD_SECONDS,
                    self._joined_at is not None,
                    bool(getattr(getattr(self._bot, "_connection", None), "is_alive", False)),
                    time.monotonic() - self._last_irc_line,
                )
                return

    async def _refresh_token(self) -> None:
        if self._refresh_token_cb is None:
            return
        loop = asyncio.get_running_loop()
        new_token = await loop.run_in_executor(None, self._refresh_token_cb)
        if not new_token:
            return
        if new_token.startswith("oauth:"):
            new_token = new_token[len("oauth:"):]
        self._token = new_token

    async def _on_ready(self) -> None:
        """Called once the IRC connection is established and the channel is joined."""
        # The one place that may set _joined_at: twitchio dispatches ready only
        # after the channel JOIN is confirmed. Note it is NOT re-dispatched after
        # an internal reconnect (the call is commented out in twitchio 2.10), so
        # a reconnect correctly leaves us "not connected" until run() rebuilds.
        self._joined_at = time.monotonic()
        self._last_irc_line = time.monotonic()
        self.log.info("Twitch chat integration active on #%s.", self._channel)
        if self._quote_task is None or self._quote_task.done():
            self._quote_task = asyncio.create_task(self._quote_loop())

    # -------------------------------------------------------------------------
    # Message routing
    # -------------------------------------------------------------------------

    async def _on_message(self, message: twitchio.Message) -> None:
        username = message.author.name.lower()
        is_broadcaster = username == self._channel.lower()
        is_mod = bool(getattr(message.author, "is_mod", False)) or is_broadcaster

        # Remember how each chatter capitalises their own name, so an interaction
        # command aimed at them can use it without spending a Helix lookup.
        display = getattr(message.author, "display_name", "") or message.author.name
        self._display_names[username] = display

        # Auto-shout: fires once per session for listed users, on their first message.
        if not is_broadcaster and username not in self._seen_users:
            self._seen_users.add(username)
            asyncio.create_task(self._maybe_auto_shout(username))

        text = message.content.strip()
        text_lower = text.lower()

        # Public commands — any viewer.
        if text_lower == "!lurk":
            asyncio.create_task(self._cmd_lurk(username))
            return
        if text_lower in ("!discord", "!coda", "!commands"):
            asyncio.create_task(self._cmd_info(text_lower[1:], username))
            return
        if text_lower in ("!uptime", "!game", "!playing"):
            asyncio.create_task(self._cmd_stream_info(text_lower.lstrip("!"), username))
            return

        interaction = self._match_interaction(text_lower)
        if interaction is not None:
            verb, target = interaction
            asyncio.create_task(self._cmd_interaction(verb, username, target))
            return

        # Mod commands — broadcaster and moderators.
        if is_mod and text_lower.startswith("!so "):
            parts = text[4:].strip().split()
            if parts:
                asyncio.create_task(self._cmd_shoutout(parts[0]))
            return

        # Broadcaster-only commands.
        if not is_broadcaster:
            return

        if text_lower.startswith("!raid "):
            parts = text[6:].strip().split()
            if parts:
                asyncio.create_task(self._cmd_raid(parts[0], sub_only=False))
        elif text_lower.startswith("!subraid "):
            parts = text[9:].strip().split()
            if parts:
                asyncio.create_task(self._cmd_raid(parts[0], sub_only=True))
        elif text_lower == "!brb" and self.brb_feed is not None:
            await self.brb_feed.start()
            if self._bus_publish:
                asyncio.create_task(self._bus_publish("[TWITCH] command: brb by reburve"))
        elif text_lower == "!back" and self.brb_feed is not None:
            await self.brb_feed.stop()
            if self._bus_publish:
                asyncio.create_task(self._bus_publish("[TWITCH] command: back by reburve"))

    # -------------------------------------------------------------------------
    # Command handlers
    # -------------------------------------------------------------------------

    async def _maybe_auto_shout(self, username: str) -> None:
        """Fire a shoutout if the user is on the auto-shout list and stream is live."""
        auto_cfg = self.config.raw.get("twitch_chat", {}).get("auto_shout", {})
        if not auto_cfg.get("enabled", False):
            return
        shout_list = {c.lower() for c in auto_cfg.get("channels", [])}
        if username not in shout_list:
            return
        live_now: list[str] = self.state.get("live_now", [])
        if self._channel not in live_now:
            return
        self.log.info("Auto-shouting %s.", username)
        await self._cmd_shoutout(username)

    def _load_shoutout_template(self, username: str) -> str:
        """Return a random template block from shoutouts/{username}.txt, or the fallback.

        Template files use blank lines to separate blocks — one block is chosen at random.
        Supported variables: {login}, {display_name}, {url}, {title}, {game}
        """
        path = Path("shoutouts") / f"{username}.txt"
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
            if blocks:
                return random.choice(blocks)
        return self._cmd_cfg.get(
            "shoutout_fallback",
            ">> signal detected: {display_name} — twitch.tv/{login}",
        )

    async def _cmd_shoutout(self, username: str) -> None:
        username = username.lower().lstrip("@")
        if not username:
            return

        template = self._load_shoutout_template(username)
        needs_stream_data = "{title}" in template or "{game}" in template

        display_name = username
        title = ""
        game = ""

        if self._helix:
            try:
                users = await self._helix.get_users([username])
                user_data = users.get(username, {})
                display_name = user_data.get("display_name", username)

                if needs_stream_data and user_data:
                    streams = await self._helix.get_streams([user_data["id"]])
                    if streams:
                        title = streams[0].get("title", "")
                        game = streams[0].get("game_name", "")
            except Exception:
                self.log.exception("Helix API error during shoutout for %s.", username)

        message = template.format_map({
            "login": username,
            "display_name": display_name,
            "url": f"twitch.tv/{username}",
            "title": title,
            "game": game,
        })
        await self._send_chat(message)
        self.log.info("Shoutout posted for %s.", username)
        if self._bus_publish:
            asyncio.create_task(self._bus_publish(f"[TWITCH] command: shoutout {username} by {self._channel}"))

    async def _cmd_lurk(self, username: str) -> None:
        messages = self._cmd_cfg.get("lurk_messages", [
            "loop.trace: {user} has shifted to passive observation mode. signal confirmed.",
        ])
        template = random.choice(messages) if messages else "loop.trace: {user} acknowledged."
        await self._send_chat(template.format(user=username))
        if self._bus_publish:
            asyncio.create_task(self._bus_publish(f"[TWITCH] command: lurk by {username}"))

    @staticmethod
    def _match_interaction(text_lower: str) -> tuple[str, str] | None:
        """Parse '!hug', '!hug @someone', '!boop someone'. Returns (verb, target)
        with an empty target for the solo form, or None if not an interaction."""
        if not text_lower.startswith("!"):
            return None
        parts = text_lower[1:].split()
        if not parts or parts[0] not in INTERACTION_COMMANDS:
            return None
        target = ""
        if len(parts) > 1:
            # Cap at Twitch's max login length — the target is viewer-supplied
            # and goes straight into a chat message with a 500-char ceiling.
            target = parts[1].lstrip("@").strip(",.!?")[:25]
        return parts[0], target

    def _render(self, pool_key: str, values: dict[str, str]) -> str | None:
        """Pick a random line from the configured pool and fill it in.

        A malformed template in config.yaml is the user's typo, not a crash —
        log it and fall back to the built-in pool.
        """
        pool = self._cmd_cfg.get(f"{pool_key}_messages")
        if not isinstance(pool, list) or not pool:
            pool = DEFAULT_INTERACTION_MESSAGES.get(pool_key, [])
        if not pool:
            return None
        template = random.choice(pool)
        try:
            return template.format_map(values)
        except (KeyError, IndexError, ValueError):
            self.log.warning(
                "Bad template in %s_messages: %r — using built-in.", pool_key, template
            )
            for fallback in DEFAULT_INTERACTION_MESSAGES.get(pool_key, []):
                try:
                    return fallback.format_map(values)
                except (KeyError, IndexError, ValueError):
                    continue
            return None

    async def _cmd_interaction(self, verb: str, username: str, target: str) -> None:
        cooldown = float(
            self._cmd_cfg.get("interaction_cooldown_seconds", DEFAULT_INTERACTION_COOLDOWN)
        )
        now = time.monotonic()
        last = self._interaction_last.get(username, 0.0)
        if now - last < cooldown:
            # Silent — replying "you're on cooldown" is itself the spam.
            self.log.debug("!%s from %s ignored (cooldown).", verb, username)
            return
        self._interaction_last[username] = now

        user_display = self._display_names.get(username, username)
        # Aiming at yourself, or at nobody, both mean the solo variant.
        if target and target != username:
            pool_key = verb
            values = {
                "user": user_display,
                "target": self._display_names.get(target, target),
            }
        else:
            pool_key = f"{verb}_self"
            values = {"user": user_display, "target": user_display}

        message = self._render(pool_key, values)
        if not message:
            self.log.warning("No message pool available for %s.", pool_key)
            return
        await self._send_chat(message)
        if self._bus_publish:
            detail = f"{verb} {target} by {username}" if target else f"{verb} by {username}"
            asyncio.create_task(self._bus_publish(f"[TWITCH] command: {detail}"))

    async def _own_stream(self) -> dict[str, Any] | None:
        """Current stream payload for this channel, or None when offline."""
        if not self._helix:
            return None
        if self._broadcaster_id is None:
            users = await self._helix.get_users([self._channel.lower()])
            user = users.get(self._channel.lower(), {})
            self._broadcaster_id = user.get("id")
        if not self._broadcaster_id:
            return None
        streams = await self._helix.get_streams([self._broadcaster_id])
        return streams[0] if streams else None

    @staticmethod
    def _format_uptime(started_at: str) -> str:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - started
        total_minutes = max(int(delta.total_seconds() // 60), 0)
        hours, minutes = divmod(total_minutes, 60)
        if hours and minutes:
            return f"{hours}h {minutes}m"
        if hours:
            return f"{hours}h"
        return f"{minutes}m"

    async def _cmd_stream_info(self, cmd: str, username: str) -> None:
        if not self._helix:
            self.log.warning("!%s used but Helix is not configured.", cmd)
            return
        try:
            stream = await self._own_stream()
        except Exception:
            self.log.exception("Helix API error during !%s.", cmd)
            return

        if stream is None:
            message = self._cmd_cfg.get(
                "offline_message", "aux.proc: no active transmission. the channel is dark."
            )
        elif cmd == "uptime":
            template = self._cmd_cfg.get(
                "uptime_message", "loop.trace: signal continuous for {uptime}."
            )
            message = template.format(uptime=self._format_uptime(stream["started_at"]))
        else:
            game = stream.get("game_name", "")
            if game:
                template = self._cmd_cfg.get(
                    "game_message", "loop.trace: current environment — {game}"
                )
                message = template.format(game=game)
            else:
                message = self._cmd_cfg.get(
                    "game_unknown_message", "aux.proc: environment unclassified."
                )

        await self._send_chat(message)
        if self._bus_publish:
            asyncio.create_task(self._bus_publish(f"[TWITCH] command: {cmd} by {username}"))

    async def _cmd_raid(self, channel: str, *, sub_only: bool) -> None:
        channel = channel.lower().lstrip("#")
        key = "raid_message_sub" if sub_only else "raid_message"
        template = self._cmd_cfg.get(
            key,
            "loop.trace: signal redirecting to {channel} — twitch.tv/{channel}",
        )
        await self._send_chat(template.format(channel=channel))
        tag = "subraid" if sub_only else "raid"
        if self._bus_publish:
            asyncio.create_task(self._bus_publish(f"[TWITCH] command: {tag} {channel} by {self._channel}"))

    async def _cmd_info(self, cmd: str, username: str) -> None:
        if cmd == "discord":
            url = self._cmd_cfg.get("discord_url", "")
            if url:
                await self._send_chat(f"loop.trace: discord signal — {url}")
            else:
                self.log.warning("!discord used but discord_url is not configured.")
        elif cmd == "coda":
            msg = self._cmd_cfg.get(
                "coda_message",
                "loop.trace: CODA is an emergent signal. it observes. sometimes it speaks.",
            )
            await self._send_chat(msg)
        elif cmd == "commands":
            await self._send_chat(
                "loop.trace: available signals — !lurk  !hug  !boop  !highfive  "
                "!uptime  !game  !so  !raid  !subraid  !discord  !coda"
            )
        if self._bus_publish:
            asyncio.create_task(self._bus_publish(f"[TWITCH] command: {cmd} by {username}"))

    # -------------------------------------------------------------------------
    # Chat helpers
    # -------------------------------------------------------------------------

    async def _send_chat(self, message: str) -> None:
        if self._bot is None:
            self.log.warning("Bot not connected; cannot send chat message.")
            return
        channel = self._bot.get_channel(self._channel)
        if channel is None:
            self.log.warning("Channel #%s not found.", self._channel)
            return
        try:
            await channel.send(message)
        except Exception:
            self.log.exception("Failed to send message to chat.")

    # -------------------------------------------------------------------------
    # Quote loop (unchanged)
    # -------------------------------------------------------------------------

    async def _quote_loop(self) -> None:
        """Background loop that posts character quotes to chat while the stream is live."""
        chat_cfg = self.config.raw.get("twitch_chat", {})
        qic_cfg = chat_cfg.get("quotes_in_chat", {})
        if not qic_cfg.get("enabled", True):
            self.log.info("Chat quote posting is disabled in config.")
            return

        interval_min = int(qic_cfg.get("interval_min_seconds", 600))
        interval_max = int(qic_cfg.get("interval_max_seconds", 1200))
        startup_delay = int(qic_cfg.get("startup_delay_seconds", 120))
        buffer_size = int(qic_cfg.get("recent_buffer", 30))

        recent: deque[str] = deque(maxlen=buffer_size)
        went_live_at: float | None = None
        next_post_at: float | None = None
        was_live = False

        self.log.info(
            "Chat quote loop started (interval=%d-%ds, startup_delay=%ds).",
            interval_min,
            interval_max,
            startup_delay,
        )

        while True:
            await asyncio.sleep(10)

            live_now: list[str] = self.state.get("live_now", [])
            is_live = self._channel in live_now

            if is_live and not was_live:
                went_live_at = time.monotonic()
                next_post_at = None
                self.log.info(
                    "Stream is live; waiting %ds before first chat quote.", startup_delay
                )
            elif not is_live and was_live:
                went_live_at = None
                next_post_at = None
                self.log.info("Stream went offline; chat quote schedule reset.")

            was_live = is_live

            if not is_live or went_live_at is None:
                continue

            now = time.monotonic()

            elapsed = now - went_live_at
            if elapsed < startup_delay:
                self.log.debug(
                    "Startup delay: %.0f/%ds elapsed.", elapsed, startup_delay
                )
                continue

            if next_post_at is None:
                delay = random.uniform(interval_min, interval_max)
                next_post_at = now + delay
                self.log.info("Startup delay passed; first chat quote in %.0fs.", delay)
                continue

            if now < next_post_at:
                continue

            await self._post_chat_quote(recent)
            delay = random.uniform(interval_min, interval_max)
            next_post_at = now + delay
            self.log.debug("Next chat quote in %.0fs.", delay)

    async def _post_chat_quote(self, recent: deque[str]) -> None:
        """Pick and send a random character quote to the channel."""
        result = self._pick_chat_quote(recent)
        if result is None:
            self.log.warning("No valid quote available for Twitch chat; skipping.")
            return

        quote, character = result
        message = self._format_chat_message(character, quote)
        await self._send_chat(message)
        recent.append(quote)
        self.log.info("Posted chat quote for %s (%d chars).", character, len(message))

    def _pick_chat_quote(self, recent: deque[str]) -> tuple[str, str] | None:
        """Return (quote_text, character) or None if no valid quote is found."""
        candidates = [c for c in self.quotes if self.quotes[c]]
        if not candidates:
            return None

        weights = [self._chat_weights.get(c, 1) for c in candidates]
        recent_set = set(recent)

        # Up to 50 weighted-random attempts to find a non-recent, valid quote.
        for _ in range(50):
            character = random.choices(candidates, weights=weights, k=1)[0]
            quote = random.choice(self.quotes[character]).strip()
            if not quote or quote in recent_set:
                continue
            if not all(f(quote) for f in self._chat_filters):
                continue
            return quote, character

        # Fallback: ignore recency constraint.
        self.log.debug("Chat recent buffer saturated; falling back to any valid quote.")
        for _ in range(20):
            character = random.choices(candidates, weights=weights, k=1)[0]
            quote = random.choice(self.quotes[character]).strip()
            if quote and all(f(quote) for f in self._chat_filters):
                return quote, character

        return None

    def _format_chat_message(self, character: str, quote: str) -> str:
        """Prepend the character display name unless the quote is already self-identified."""
        display = CHAT_DISPLAY_NAMES.get(character, character)

        quote_lower = quote.lower()
        already_identified = (
            quote_lower.startswith(display.lower() + ":")
            or quote_lower.startswith(display.lower() + " ")
            or (character == "core_audit" and quote.startswith("AUDIT"))
        )

        if already_identified:
            return quote
        return f"{display}: {quote}"
