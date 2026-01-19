import logging
import os
from typing import Any

import aiohttp


class TwitchHelix:
    def __init__(self, client_id: str, client_secret: str, session: aiohttp.ClientSession) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session
        self.log = logging.getLogger("twitch.helix")
        self._token: str | None = None

    async def get_app_token(self) -> str:
        if self._token:
            return self._token
        url = "https://id.twitch.tv/oauth2/token"
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        async with self.session.post(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
        self._token = data["access_token"]
        self.log.info("Obtained app access token.")
        return self._token

    async def _request(self, method: str, url: str, params: dict[str, Any] | None = None,
                       json: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self.get_app_token()
        headers = {"Client-ID": self.client_id, "Authorization": f"Bearer {token}"}
        async with self.session.request(method, url, headers=headers, params=params, json=json) as resp:
            if resp.status == 401:
                self._token = None
                token = await self.get_app_token()
                headers["Authorization"] = f"Bearer {token}"
                async with self.session.request(
                    method, url, headers=headers, params=params, json=json
                ) as retry_resp:
                    retry_resp.raise_for_status()
                    return await retry_resp.json()
            resp.raise_for_status()
            return await resp.json()
        
    async def _request_user(self, method: str, url: str, params=None, json=None):
        token = (os.environ.get("TWITCH_USER_ACCESS_TOKEN") or "").strip()
        if not token:
            raise RuntimeError(
                "Missing TWITCH_USER_ACCESS_TOKEN. "
                "EventSub WebSocket subscriptions require a USER access token."
            )
        headers = {"Client-ID": self.client_id, "Authorization": f"Bearer {token}"}
        async with self.session.request(method, url, headers=headers, params=params, json=json) as resp:
            if resp.status >= 400:
                body = await resp.text()
                self.log.error("Helix(user) error %s %s -> HTTP %s body=%s", method, url, resp.status, body)
            resp.raise_for_status()
            return await resp.json()


    async def get_users(self, logins: list[str]) -> dict[str, dict[str, Any]]:
        data = await self._request(
            "GET",
            "https://api.twitch.tv/helix/users",
            params=[("login", login) for login in logins],
        )
        return {item["login"].lower(): item for item in data.get("data", [])}

    async def get_stream(self, user_id: str) -> dict[str, Any] | None:
        data = await self._request(
            "GET", "https://api.twitch.tv/helix/streams", params={"user_id": user_id}
        )
        streams = data.get("data", [])
        return streams[0] if streams else None

    async def create_eventsub_subscription(
        self, session_id: str, broadcaster_id: str, event_type: str
    ) -> None:
        payload = {
            "type": event_type,
            "version": "1",
            "condition": {"broadcaster_user_id": broadcaster_id},
            "transport": {"method": "websocket", "session_id": session_id},
        }
        await self._request_user(
            "POST", "https://api.twitch.tv/helix/eventsub/subscriptions", json=payload
        )
        self.log.info(
            "Subscribed to %s for broadcaster_id=%s", event_type, broadcaster_id
        )
