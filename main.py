import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from config import load_config
from discord_webhook import DiscordWebhook
from twitch_helix import TwitchHelix
from twitch_polling import Poller


STATE_PATH = Path("state.json")


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        with STATE_PATH.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    state = {}
    save_state(state)
    return state


def save_state(state: dict[str, Any]) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config("config.yaml")
    state = load_state()

    async with aiohttp.ClientSession() as session:
        helix = TwitchHelix(
            config.twitch["client_id"], config.twitch["client_secret"], session
        )
        webhook = DiscordWebhook(session)

        polling_config = config.raw.get("polling", {})
        poller_task = None
        if polling_config.get("enabled", True):
            poller = Poller(
                config=config,
                helix=helix,
                webhook=webhook,
                state=state,
                save_state=save_state,
                interval_seconds=int(polling_config.get("interval_seconds", 90)),
            )
            poller_task = asyncio.create_task(poller.run())
        quote_task = None
        quotes_config = config.raw.get("quotes", {})
        if quotes_config.get("enabled", False):
            from quote_drip import QuoteDrip

            quote_drip = QuoteDrip(
                quotes_config=quotes_config,
                characters=config.discord.get("characters", {}),
                webhook=webhook,
                state=state,
                save_state=save_state,
            )
            quote_task = asyncio.create_task(quote_drip.run())
        if poller_task and quote_task:
            await asyncio.gather(poller_task, quote_task)
        elif poller_task:
            await poller_task
        elif quote_task:
            await quote_task
        else:
            logging.warning("Polling is disabled and no other tasks enabled.")


if __name__ == "__main__":
    asyncio.run(main())
