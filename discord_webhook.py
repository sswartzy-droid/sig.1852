import asyncio
import logging
from typing import Any

import aiohttp


class DiscordWebhook:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.log = logging.getLogger("discord")

    async def send(self, webhook_url: str, content: str) -> None:
        payload = {"content": content}
        attempts = 0
        while True:
            attempts += 1
            async with self.session.post(webhook_url, json=payload) as resp:
                if resp.status == 204 or resp.status == 200:
                    self.log.info("Discord webhook sent.")
                    return
                if resp.status == 429:
                    data = await resp.json()
                    retry_after = float(
                        resp.headers.get("Retry-After", data.get("retry_after", 1))
                    )
                    self.log.warning(
                        "Discord rate limited. Retrying after %.2fs", retry_after
                    )
                    await asyncio.sleep(retry_after)
                    continue
                text = await resp.text()
                self.log.error(
                    "Discord webhook failed status=%s body=%s", resp.status, text
                )
                if attempts >= 3:
                    return
                await asyncio.sleep(1)

