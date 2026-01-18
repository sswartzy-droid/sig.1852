import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import aiohttp

from twitch_helix import TwitchHelix


EventHandler = Callable[[str, dict], Awaitable[None]]


class TwitchEventSub:
    def __init__(
        self,
        helix: TwitchHelix,
        session: aiohttp.ClientSession,
        broadcaster_ids: list[str],
        handler: EventHandler,
        subscribe_online: bool = True,
        subscribe_offline: bool = False,
    ) -> None:
        self.helix = helix
        self.session = session
        self.broadcaster_ids = broadcaster_ids
        self.handler = handler
        self.subscribe_online = subscribe_online
        self.subscribe_offline = subscribe_offline
        self.log = logging.getLogger("twitch.eventsub")

    async def run_forever(self) -> None:
        while True:
            try:
                await self._connect_once()
            except Exception:
                self.log.exception("EventSub connection error. Reconnecting soon.")
            await asyncio.sleep(5)

    async def _connect_once(self) -> None:
        url = "wss://eventsub.wss.twitch.tv/ws"
        self.log.info("Connecting to Twitch EventSub websocket.")
        async with self.session.ws_connect(url, heartbeat=20) as ws:
            session_id = None
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    message_type = data.get("metadata", {}).get("message_type")
                    if message_type == "session_welcome":
                        session_id = data["payload"]["session"]["id"]
                        self.log.info("EventSub session established: %s", session_id)
                        await self._subscribe_all(session_id)
                    elif message_type == "notification":
                        event = data["payload"]["event"]
                        subscription_type = data["payload"]["subscription"]["type"]
                        await self.handler(subscription_type, event)
                    elif message_type == "session_keepalive":
                        self.log.debug("EventSub keepalive received.")
                    elif message_type == "session_reconnect":
                        reconnect_url = data["payload"]["session"]["reconnect_url"]
                        self.log.warning("EventSub requested reconnect.")
                        await ws.close()
                        await self._reconnect(reconnect_url)
                        return
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self.log.error("EventSub websocket error: %s", msg.data)
                    return

    async def _subscribe_all(self, session_id: str) -> None:
        for broadcaster_id in self.broadcaster_ids:
            if self.subscribe_online:
                await self.helix.create_eventsub_subscription(
                    session_id, broadcaster_id, "stream.online"
                )
            if self.subscribe_offline:
                await self.helix.create_eventsub_subscription(
                    session_id, broadcaster_id, "stream.offline"
                )

    async def _reconnect(self, reconnect_url: str) -> None:
        self.log.info("Connecting to reconnect URL.")
        async with self.session.ws_connect(reconnect_url, heartbeat=20) as ws:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    message_type = data.get("metadata", {}).get("message_type")
                    if message_type == "session_welcome":
                        session_id = data["payload"]["session"]["id"]
                        self.log.info("EventSub session re-established: %s", session_id)
                        await self._subscribe_all(session_id)
                    elif message_type == "notification":
                        event = data["payload"]["event"]
                        subscription_type = data["payload"]["subscription"]["type"]
                        await self.handler(subscription_type, event)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self.log.error("EventSub websocket error: %s", msg.data)
                    return
