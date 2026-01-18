import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from config import load_config
from discord_webhook import DiscordWebhook
from twitch_eventsub import TwitchEventSub
from twitch_helix import TwitchHelix


STATE_PATH = Path("state.json")


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        with STATE_PATH.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def save_state(state: dict[str, Any]) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def format_message(template: str, **kwargs: Any) -> str:
    return template.format(**kwargs)


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

        logins = [channel["login"].lower() for channel in config.channels]
        user_map = await helix.get_users(logins)
        if len(user_map) != len(logins):
            missing = set(logins) - set(user_map.keys())
            logging.warning("Missing Twitch users: %s", ", ".join(sorted(missing)))

        channel_info = {}
        for channel in config.channels:
            login = channel["login"].lower()
            user = user_map.get(login)
            if not user:
                continue
            channel_info[login] = {
                "id": user["id"],
                "login": login,
                "display_name": user.get("display_name", login),
                "character": channel.get("character"),
                "announce_online": channel.get("announce_online", True),
                "announce_offline": channel.get("announce_offline", False),
                "template_online": channel.get(
                    "template_online",
                    "{display_name} is live! {url}",
                ),
                "template_offline": channel.get(
                    "template_offline",
                    "{display_name} went offline.",
                ),
            }

        broadcaster_ids = [info["id"] for info in channel_info.values()]
        subscribe_online = any(info["announce_online"] for info in channel_info.values())
        subscribe_offline = any(info["announce_offline"] for info in channel_info.values())

        async def handle_event(event_type: str, event: dict[str, Any]) -> None:
            login = event.get("broadcaster_user_login", "").lower()
            info = channel_info.get(login)
            if not info:
                return

            last_state = state.get(login, {"live": False})
            url = f"https://twitch.tv/{login}"

            if event_type == "stream.online":
                if last_state.get("live"):
                    logging.info("Ignoring duplicate online event for %s", login)
                    return
                if info["announce_online"]:
                    stream = await helix.get_stream(info["id"])
                    title = stream.get("title") if stream else ""
                    game = stream.get("game_name") if stream else ""
                    message = format_message(
                        info["template_online"],
                        login=login,
                        display_name=info["display_name"],
                        url=url,
                        title=title,
                        game=game,
                    )
                    await webhook.send(resolve_webhook(config, info), message)
                state[login] = {"live": True}
                save_state(state)
                logging.info("Marked %s as live.", login)
            elif event_type == "stream.offline":
                if not last_state.get("live"):
                    logging.info("Ignoring duplicate offline event for %s", login)
                    return
                if info["announce_offline"]:
                    message = format_message(
                        info["template_offline"],
                        login=login,
                        display_name=info["display_name"],
                        url=url,
                    )
                    await webhook.send(resolve_webhook(config, info), message)
                state[login] = {"live": False}
                save_state(state)
                logging.info("Marked %s as offline.", login)

        eventsub = TwitchEventSub(
            helix=helix,
            session=session,
            broadcaster_ids=broadcaster_ids,
            handler=handle_event,
            subscribe_online=subscribe_online,
            subscribe_offline=subscribe_offline,
        )
        await eventsub.run_forever()


def resolve_webhook(config: Any, info: dict[str, Any]) -> str:
    character = info.get("character")
    if character:
        return config.discord["characters"][character]
    return config.discord["system_webhook"]


if __name__ == "__main__":
    asyncio.run(main())
