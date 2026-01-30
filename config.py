import os
import re
from dataclasses import dataclass
from typing import Any

import yaml


ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def replacer(match: re.Match[str]) -> str:
            return os.getenv(match.group(1), "")

        return ENV_PATTERN.sub(replacer, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(val) for key, val in value.items()}
    return value


@dataclass
class AppConfig:
    raw: dict[str, Any]
    config_path: str = "config.yaml"

    @property
    def twitch(self) -> dict[str, Any]:
        return self.raw["twitch"]

    @property
    def discord(self) -> dict[str, Any]:
        return self.raw["discord"]

    @property
    def channels(self) -> list[dict[str, Any]]:
        return self.raw["channels"]

    def get_mtime(self) -> float:
        return os.path.getmtime(self.config_path)

    def reload(self) -> "AppConfig":
        return load_config(self.config_path)


def validate_config(data: dict[str, Any]) -> None:
    errors: list[str] = []
    twitch = data.get("twitch")
    if not isinstance(twitch, dict):
        errors.append("Missing 'twitch' section.")
    else:
        if not twitch.get("client_id"):
            errors.append("twitch.client_id is empty (check TWITCH_CLIENT_ID env var).")
        if not twitch.get("client_secret"):
            errors.append("twitch.client_secret is empty (check TWITCH_CLIENT_SECRET env var).")

    discord = data.get("discord")
    if not isinstance(discord, dict):
        errors.append("Missing 'discord' section.")
    elif not discord.get("system_webhook"):
        errors.append("discord.system_webhook is empty (check DISCORD_WEBHOOK_SYSTEM env var).")

    channels = data.get("channels")
    if not isinstance(channels, list) or len(channels) == 0:
        errors.append("No channels configured.")

    if errors:
        raise ValueError("Config validation failed:\n  - " + "\n  - ".join(errors))


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    expanded = _expand_env(data)
    validate_config(expanded)
    return AppConfig(raw=expanded, config_path=path)
