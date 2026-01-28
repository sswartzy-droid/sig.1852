import asyncio
import json
import logging
import os
import signal
import tempfile
from pathlib import Path
from typing import Any

import aiohttp

from config import load_config
from discord_webhook import DiscordWebhook
from twitch_helix import TwitchHelix
from twitch_polling import Poller

STATE_DIR = Path(os.getenv("STATE_DIR", "data"))
STATE_PATH = STATE_DIR / "state.json"

log = logging.getLogger("main")


def load_state() -> dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        with STATE_PATH.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    state: dict[str, Any] = {}
    save_state(state)
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp_path, STATE_PATH)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def main() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config("config.yaml")
    state = load_state()

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("Shutdown signal received, saving state and exiting...")
        save_state(state)
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    async with aiohttp.ClientSession() as session:
        helix = TwitchHelix(
            config.twitch["client_id"], config.twitch["client_secret"], session
        )
        webhook = DiscordWebhook(session)

        tasks: list[asyncio.Task] = []

        polling_config = config.raw.get("polling", {})
        if polling_config.get("enabled", True):
            poller = Poller(
                config=config,
                helix=helix,
                webhook=webhook,
                state=state,
                save_state=save_state,
                interval_seconds=int(polling_config.get("interval_seconds", 90)),
            )
            tasks.append(asyncio.create_task(poller.run()))

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
            tasks.append(asyncio.create_task(quote_drip.run()))

        if not tasks:
            log.warning("No tasks enabled (polling disabled, quotes disabled).")
            return

        shutdown_task = asyncio.create_task(shutdown_event.wait())
        done, pending = await asyncio.wait(
            [*tasks, shutdown_task], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass

        save_state(state)
        log.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
