import asyncio
import logging
from typing import Any

import aiohttp

MAX_RATE_LIMIT_RETRIES = 5
MAX_ERROR_RETRIES = 3


class WebhookSendError(Exception):
    """Raised when a Discord webhook message fails after all retries."""


class DiscordWebhook:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.log = logging.getLogger("discord")

    async def send(self, webhook_url: str, content: str) -> None:
        payload = {"content": content}
        error_attempts = 0
        rate_limit_attempts = 0
        while True:
            async with self.session.post(webhook_url, json=payload) as resp:
                if resp.status in (200, 204):
                    self.log.info("Discord webhook sent.")
                    return
                if resp.status == 429:
                    rate_limit_attempts += 1
                    if rate_limit_attempts >= MAX_RATE_LIMIT_RETRIES:
                        raise WebhookSendError(
                            f"Discord rate limited {rate_limit_attempts} times; giving up."
                        )
                    data = await resp.json()
                    retry_after = float(
                        resp.headers.get("Retry-After", data.get("retry_after", 1))
                    )
                    self.log.warning(
                        "Discord rate limited (%d/%d). Retrying after %.2fs",
                        rate_limit_attempts,
                        MAX_RATE_LIMIT_RETRIES,
                        retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                text = await resp.text()
                self.log.error(
                    "Discord webhook failed status=%s body=%s", resp.status, text
                )
                error_attempts += 1
                if error_attempts >= MAX_ERROR_RETRIES:
                    raise WebhookSendError(
                        f"Discord webhook failed after {error_attempts} attempts "
                        f"(last status={resp.status})."
                    )
                await asyncio.sleep(1)
