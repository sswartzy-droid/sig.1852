import asyncio
import logging
from typing import Any, Callable

from discord_webhook import DiscordWebhook
from twitch_helix import TwitchHelix


class Poller:
    def __init__(
        self,
        config: Any,
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
        while True:
            await self._poll_once()
            await asyncio.sleep(self.interval_seconds)

    def _ensure_state_shape(self) -> None:
        self.state.setdefault("last_started_at_announced", {})
        live_now = self.state.get("live_now")
        if live_now is None:
            legacy_live = [
                login
                for login, value in self.state.items()
                if isinstance(value, dict) and value.get("live") is True
            ]
            self.state["live_now"] = legacy_live
            self.save_state(self.state)
        elif isinstance(live_now, dict):
            self.state["live_now"] = [login for login, is_live in live_now.items() if is_live]
            self.save_state(self.state)

    async def _poll_once(self) -> None:
        if not self.id_map:
            self.log.warning("No valid Twitch channels configured; skipping poll.")
            return
        try:
            streams = await self._fetch_live_streams()
            await self._handle_streams(streams)
        except Exception:
            self.log.exception("Polling error; retrying after interval.")

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
        self.save_state(self.state)

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
        try:
            await self.webhook.send(self._resolve_webhook(info), message)
        except Exception:
            self.log.exception("Failed to send Discord announcement for %s", login)

    def _resolve_webhook(self, info: dict[str, Any]) -> str:
        character = info.get("character")
        if character:
            return self.config.discord["characters"][character]
        return self.config.discord["system_webhook"]

    @staticmethod
    def _format_message(template: str, **kwargs: Any) -> str:
        return template.format(**kwargs)
