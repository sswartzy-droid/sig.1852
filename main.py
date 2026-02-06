import asyncio
import json
import logging
import logging.handlers
import os
import signal
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from config import load_config
from discord_webhook import DiscordWebhook
from twitch_helix import TwitchHelix
from twitch_polling import Poller

STATE_DIR = Path(os.getenv("STATE_DIR", "data"))
STATE_PATH = STATE_DIR / "state.json"

log = logging.getLogger("main")

# GMT-6 (Central Standard Time)
CST = timezone(timedelta(hours=-6))


def _safe_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


HEALTH_PORT = _safe_int_env("HEALTH_PORT", 8080)
HEALTH_STALE_SECONDS = _safe_int_env("HEALTH_STALE_SECONDS", 300)
HEALTH_HOST = os.getenv("HEALTH_HOST", "127.0.0.1")
CONFIG_PATH = os.getenv("CONFIG_PATH", "config.yaml")


def load_state() -> dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        try:
            with STATE_PATH.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, ValueError):
            log.warning("Corrupted state.json detected; starting with empty state.")
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


def _format_duration(seconds: float) -> str:
    """Format seconds as human-readable duration like '2h 15m' or '45m 30s'."""
    if seconds < 0:
        return "now"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_timestamp(ts: float) -> str:
    """Format Unix timestamp as ISO 8601 in CST (GMT-6)."""
    dt = datetime.fromtimestamp(ts, tz=CST)
    return dt.strftime("%Y-%m-%d %H:%M:%S CST")


async def _start_health_server(
    state: dict[str, Any], started_at: float, channel_count: int
) -> web.AppRunner:
    async def _health_handler(request: web.Request) -> web.Response:
        now = time.time()
        uptime = now - started_at
        last_poll = state.get("last_poll_at", 0)
        poll_age = now - last_poll if last_poll else None

        # Grace period: report healthy during first HEALTH_STALE_SECONDS after start
        if poll_age is not None:
            healthy = poll_age < HEALTH_STALE_SECONDS
        else:
            healthy = uptime < HEALTH_STALE_SECONDS

        quotes = state.get("quotes", {})
        live_now = state.get("live_now", [])
        last_announced = state.get("last_started_at_announced", {})
        next_post_ts = quotes.get("next_post_at")

        # Build human-readable time until next quote
        if next_post_ts:
            time_until = next_post_ts - now
            if time_until > 0:
                next_quote_in = _format_duration(time_until)
            else:
                next_quote_in = "pending"
        else:
            next_quote_in = None

        body = json.dumps({
            "status": "ok" if healthy else "stale",
            "server_time": _format_timestamp(now),
            "uptime": _format_duration(uptime),
            "uptime_seconds": round(uptime, 1),
            "polling": {
                "last_poll_at": _format_timestamp(last_poll) if last_poll else None,
                "poll_age": _format_duration(poll_age) if poll_age else None,
                "poll_age_seconds": round(poll_age, 1) if poll_age is not None else None,
                "channels_monitored": channel_count,
                "channels_live": live_now,
                "channels_live_count": len(live_now),
                "last_announced": list(last_announced.keys()),
            },
            "quotes": {
                "date": quotes.get("date"),
                "posted_today": quotes.get("daily_posted", 0),
                "quota_today": quotes.get("daily_quota", 0),
                "next_post_at": _format_timestamp(next_post_ts) if next_post_ts else None,
                "next_post_in": next_quote_in,
            },
        }, indent=2)
        return web.Response(
            status=200 if healthy else 503,
            text=body,
            content_type="application/json",
        )

    async def _root_handler(request: web.Request) -> web.Response:
        return web.Response(
            status=200,
            text='{"service": "sig.1852", "endpoints": ["/health"]}',
            content_type="application/json",
        )

    app = web.Application()
    app.router.add_get("/", _root_handler)
    app.router.add_get("/health", _health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HEALTH_HOST, HEALTH_PORT)
    await site.start()
    log.info("Health endpoint listening on %s:%d", HEALTH_HOST, HEALTH_PORT)
    return runner


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def _setup_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)
    log_format = os.getenv("LOG_FORMAT", "text").lower()
    log_file = os.getenv("LOG_FILE", "")

    root = logging.getLogger()
    root.setLevel(level)

    if log_format == "json":
        formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


async def main() -> None:
    _setup_logging()
    config = load_config(CONFIG_PATH)
    state = load_state()

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        log.info("Shutdown signal received...")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    health_runner = await _start_health_server(state, time.time(), len(config.channels))

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

        await health_runner.cleanup()
        save_state(state)
        log.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
