import logging
import time
from typing import Any

import aiohttp


class TwitchHelix:
    def __init__(self, client_id: str, client_secret: str, session: aiohttp.ClientSession) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session
        self.log = logging.getLogger("twitch.helix")
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def get_app_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
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
        expires_in = int(data.get("expires_in", 3600))
        # Refresh 5 minutes before actual expiry
        self._token_expires_at = time.monotonic() + max(expires_in - 300, 60)
        self.log.info("Obtained app access token (expires in %ds).", expires_in)
        return self._token

    async def _request(self, method: str, url: str, params: dict[str, Any] | list | None = None,
                       json: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self.get_app_token()
        headers = {"Client-ID": self.client_id, "Authorization": f"Bearer {token}"}
        async with self.session.request(method, url, headers=headers, params=params, json=json) as resp:
            if resp.status == 401:
                self._token = None
                self._token_expires_at = 0.0
                token = await self.get_app_token()
                headers["Authorization"] = f"Bearer {token}"
                async with self.session.request(
                    method, url, headers=headers, params=params, json=json
                ) as retry_resp:
                    retry_resp.raise_for_status()
                    return await retry_resp.json()
            resp.raise_for_status()
            return await resp.json()

    async def get_users(self, logins: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for idx in range(0, len(logins), 100):
            batch = logins[idx : idx + 100]
            data = await self._request(
                "GET",
                "https://api.twitch.tv/helix/users",
                params=[("login", login) for login in batch],
            )
            for item in data.get("data", []):
                result[item["login"].lower()] = item
        return result

    async def get_streams(self, user_ids: list[str]) -> list[dict[str, Any]]:
        if not user_ids:
            return []
        data = await self._request(
            "GET",
            "https://api.twitch.tv/helix/streams",
            params=[("user_id", user_id) for user_id in user_ids],
        )
        return data.get("data", [])
