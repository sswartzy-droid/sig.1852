import asyncio
import logging
from typing import Any, Callable

import twitchio
from twitchio.ext import commands

from brb_feed import BrbFeed
from config import AppConfig
from discord_webhook import DiscordWebhook


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
    ) -> None:
        self.config = config
        self.webhook = discord_webhook
        self.quotes = quotes
        self.state = state
        self.save_state = save_state
        self.brb_feed = brb_feed
        self.log = logging.getLogger("twitch.chat")

        chat_cfg = config.raw.get("twitch_chat", {})
        token: str = chat_cfg.get("token", "")
        # Strip oauth: prefix if present — twitchio adds it internally for IRC.
        if token.startswith("oauth:"):
            token = token[len("oauth:"):]
        self._token = token
        self._channel: str = chat_cfg.get("channel", "reburve")

        self._bot: _Bot | None = None
        # Per-session seen-user set — in-memory only, intentionally not persisted.
        self._seen_users: set[str] = set()

    async def run(self) -> None:
        if not self._token:
            self.log.error(
                "TWITCH_CHAT_TOKEN is not set; Twitch chat integration will not start."
            )
            return
        self._bot = _Bot(token=self._token, channel=self._channel, parent=self)
        self.log.info("Connecting to Twitch IRC, joining #%s...", self._channel)
        await self._bot.start()

    async def _on_ready(self) -> None:
        """Called once the IRC connection is established and the channel is joined."""
        self.log.info("Twitch chat integration active on #%s.", self._channel)

    async def _on_message(self, message: twitchio.Message) -> None:
        """Called for every non-echo incoming chat message."""
        # Command handlers and auto-shoutout will be added here.
        pass
