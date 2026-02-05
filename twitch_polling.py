import asyncio
import logging
import time
from typing import Any, Callable

import aiohttp

from config import AppConfig
from discord_webhook import DiscordWebhook
from twitch_helix import TwitchHelix

MAX_BACKOFF_MULTIPLIER = 8


class _SafeDict(dict):
    """dict subclass that returns the key as '{key}' for missing lookups."""

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


class Poller:
    def __init__(
        self,
        config: AppConfig,
        helix: TwitchHelix,
        webhook: DiscordWebhook,
        state: dict[str, Any],
        save_state: Callable[[dict[str, Any]], None],
        interval_seconds: int = 90,
    ) -> None:
        self.config = config
        self.helix = helix
        self.webhook = webhook
        self.state = state
        self.save_state = save_state
        self.interval_seconds = interval_seconds
        self.log = logging.getLogger("twitch.polling")
        self.channel_info: dict[str, dict[str, Any]] = {}
        self.id_map: dict[str, dict[str, Any]] = {}
        self._consecutive_failures = 0

    async def initialize(self) -> None:
        logins = [channel["login"].lower() for channel in self.config.channels]
        user_map = await self.helix.get_users(logins)
        if len(user_map) != len(logins):
            missing = set(logins) - set(user_map.keys())
            self.log.warning("Missing Twitch users: %s", ", ".join(sorted(missing)))

        for channel in self.config.channels:
            login = channel["login"].lower()
            user = user_map.get(login)
            if not user:
                continue
            info = {
                "id": user["id"],
                "login": login,
                "display_name": user.get("display_name", login),
                "character": channel.get("character"),
                "announce_online": channel.get("announce_online", True),
                "template_online": channel.get(
                    "template_online",
                    "{display_name} is live! {url}",
                ),
            }
            self.channel_info[login] = info
            self.id_map[user["id"]] = info

    async def run(self) -> None:
        await self.initialize()
        self._ensure_state_shape()
        self._config_mtime = self.config.get_mtime()
        while True:
            await self._check_config_reload()
            await self._poll_once()
            sleep = self._next_sleep()
            await asyncio.sleep(sleep)

    async def _check_config_reload(self) -> None:
        try:
            current_mtime = self.config.get_mtime()
        except OSError:
            return
        if current_mtime <= self._config_mtime:
            return
        self.log.info("Config file changed, reloading...")
        try:
            new_config = self.config.reload()
            self.config = new_config
            self.channel_info.clear()
            self.id_map.clear()
            await self.initialize()
            self._config_mtime = current_mtime
            self.log.info("Config reloaded: %d channels.", len(self.channel_info))
        except ValueError:
            self.log.exception("Config validation failed on reload; keeping current config.")
        except Exception:
            self.log.exception("Failed to reload config; keeping current.")

    def _next_sleep(self) -> float:
        if self._consecutive_failures <= 0:
            return self.interval_seconds
        multiplier = min(2 ** self._consecutive_failures, MAX_BACKOFF_MULTIPLIER)
        backed_off = self.interval_seconds * multiplier
        self.log.info(
            "Backing off: %d consecutive failures, sleeping %ds.",
            self._consecutive_failures,
            backed_off,
        )
        return backed_off

    def _ensure_state_shape(self) -> None:
        self.state.setdefault("last_started_at_announced", {})
        if self.state.get("live_now") is None:
            self.state["live_now"] = []
            self.save_state(self.state)

    async def _poll_once(self) -> None:
        if not self.id_map:
            self.log.warning("No valid Twitch channels configured; skipping poll.")
            return
        try:
            streams = await self._fetch_live_streams()
            await self._handle_streams(streams)
            self._consecutive_failures = 0
            self.state["last_poll_at"] = time.time()
        except aiohttp.ClientError:
            self._consecutive_failures += 1
            self.log.exception(
                "Network error during poll (failure #%d); will retry.",
                self._consecutive_failures,
            )
        except Exception:
            self._consecutive_failures += 1
            self.log.exception(
                "Unexpected polling error (failure #%d); will retry.",
                self._consecutive_failures,
            )
        finally:
            self.save_state(self.state)

    async def _fetch_live_streams(self) -> list[dict[str, Any]]:
        broadcaster_ids = list(self.id_map.keys())
        streams: list[dict[str, Any]] = []
        for idx in range(0, len(broadcaster_ids), 100):
            batch = broadcaster_ids[idx : idx + 100]
            streams.extend(await self.helix.get_streams(batch))
        return streams

    async def _handle_streams(self, streams: list[dict[str, Any]]) -> None:
        live_lookup = {stream["user_id"]: stream for stream in streams}
        last_started = self.state.setdefault("last_started_at_announced", {})
        current_live_logins = set()

        for broadcaster_id, stream in live_lookup.items():
            info = self.id_map.get(broadcaster_id)
            if not info:
                continue
            login = info["login"]
            current_live_logins.add(login)
            started_at = stream.get("started_at")
            if started_at and started_at != last_started.get(login):
                await self._announce_live(info, stream)
                last_started[login] = started_at

        self.state["live_now"] = sorted(current_live_logins)

    async def _announce_live(self, info: dict[str, Any], stream: dict[str, Any]) -> None:
        if not info.get("announce_online", True):
            return
        login = info["login"]
        url = f"https://twitch.tv/{login}"
        message = self._format_message(
            info["template_online"],
            login=login,
            display_name=info["display_name"],
            url=url,
            title=stream.get("title", ""),
            game=stream.get("game_name", ""),
        )
        webhook_url = self._resolve_webhook(info)
        try:
            await self.webhook.send(webhook_url, message)
        except Exception:
            self.log.exception("Failed to send Discord announcement for %s", login)

    def _resolve_webhook(self, info: dict[str, Any]) -> str:
        character = info.get("character")
        if character:
            characters = self.config.discord.get("characters", {})
            webhook = characters.get(character)
            if webhook:
                return webhook
            self.log.warning(
                "Unknown character '%s'; falling back to system_webhook.", character
            )
        return self.config.discord["system_webhook"]

    @staticmethod
    def _format_message(template: str, **kwargs: Any) -> str:
        return template.format_map(_SafeDict(**kwargs))
