import asyncio
import logging
import time
from typing import Any

from discord_webhook import DiscordWebhook
from twitch_chat import CHAT_DISPLAY_NAMES


class LoreForward:
    """Occasionally mirrors a delivered CODA stream line to that character's Discord webhook.

    Fed from the /say endpoint, so the lore channel only ever receives lines that
    actually landed in Twitch chat. Throttled to preserve the channel's slow-drip
    cadence alongside the daily quote drip.
    """

    def __init__(
        self,
        webhook: DiscordWebhook,
        characters: dict[str, str],
        lore_config: dict[str, Any],
    ) -> None:
        self.webhook = webhook
        self.characters = characters
        self.min_interval = float(lore_config.get("min_interval_minutes", 30)) * 60
        self.log = logging.getLogger("lore.forward")
        self._next_at = 0.0

    def maybe_post(self, character: str, message: str) -> None:
        url = self.characters.get(character, "")
        if not url:
            return
        now = time.monotonic()
        if now < self._next_at:
            return
        self._next_at = now + self.min_interval

        # The webhook already speaks as the character — drop a self-identifying prefix.
        display = CHAT_DISPLAY_NAMES.get(character, character)
        text = message.strip()
        if text.lower().startswith(display.lower() + ":"):
            text = text[len(display) + 1:].strip()
        if not text:
            return

        asyncio.create_task(self._post(character, url, text))

    async def _post(self, character: str, url: str, text: str) -> None:
        try:
            await self.webhook.send(url, text)
            self.log.info("Lore line forwarded for %s (%d chars).", character, len(text))
        except Exception:
            self.log.exception("Lore forward failed for %s.", character)
