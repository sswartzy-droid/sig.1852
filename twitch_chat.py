import asyncio
import logging
import random
import time
from collections import deque
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
        # Per-session seen-user set — tracks who has been seen this stream for auto-shout.
        self._seen_users: set[str] = set()

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

    async def run(self) -> None:
        if not self._token:
            self.log.warning(
                "TWITCH_CHAT_TOKEN is not set; chat integration disabled. "
                "Other services will continue running."
            )
            # Block forever so FIRST_COMPLETED doesn't trigger shutdown.
            await asyncio.Event().wait()
            return
        self._bot = _Bot(token=self._token, channel=self._channel, parent=self)
        self.log.info("Connecting to Twitch IRC, joining #%s...", self._channel)
        await self._bot.start()

    async def _on_ready(self) -> None:
        """Called once the IRC connection is established and the channel is joined."""
        self.log.info("Twitch chat integration active on #%s.", self._channel)
        asyncio.create_task(self._quote_loop())

    # -------------------------------------------------------------------------
    # Message routing
    # -------------------------------------------------------------------------

    async def _on_message(self, message: twitchio.Message) -> None:
        username = message.author.name.lower()
        is_broadcaster = username == self._channel.lower()
        is_mod = bool(getattr(message.author, "is_mod", False)) or is_broadcaster

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
                "loop.trace: available signals — !lurk  !so  !raid  !subraid  !discord  !coda"
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
