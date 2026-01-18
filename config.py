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

    @property
    def twitch(self) -> dict[str, Any]:
        return self.raw["twitch"]

    @property
    def discord(self) -> dict[str, Any]:
        return self.raw["discord"]

    @property
    def channels(self) -> list[dict[str, Any]]:
        return self.raw["channels"]


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    expanded = _expand_env(data)
    return AppConfig(raw=expanded)
