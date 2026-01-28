import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from discord_webhook import DiscordWebhook


class QuoteDrip:
    def __init__(
        self,
        quotes_config: dict,
        characters: dict[str, str],
        webhook: DiscordWebhook,
        state: dict,
        save_state: Callable[[dict], None],
    ) -> None:
        self.quotes_config = quotes_config
        self.characters = characters
        self.webhook = webhook
        self.state = state
        self.save_state = save_state
        self.log = logging.getLogger("quotes")
        self.quotes_dir = Path(quotes_config.get("quotes_dir", "quotes"))
        self.daily_min = int(quotes_config.get("daily_min", 1))
        self.daily_max = int(quotes_config.get("daily_max", 3))
        self.max_sentences = int(quotes_config.get("max_sentences", 3))
        self.max_chars = int(quotes_config.get("max_chars", 350))
        self.no_links = bool(quotes_config.get("no_links", True))
        self.no_mentions = bool(quotes_config.get("no_mentions", True))
        self.window_start = self._parse_time(quotes_config.get("window_start"))
        self.window_end = self._parse_time(quotes_config.get("window_end"))
        self.quotes = self._load_quotes()

    @staticmethod
    def _parse_time(value: str | None) -> tuple[int, int] | None:
        if not value:
            return None
        parts = str(value).split(":")
        return (int(parts[0]), int(parts[1]))

    async def run(self) -> None:
        if not self.quotes:
            self.log.warning("No quotes loaded; quote drip disabled.")
            return
        while True:
            self._ensure_daily_state()
            quote_state = self.state.setdefault("quotes", {})
            now = time.time()
            next_post_at = quote_state.get("next_post_at")

            if next_post_at is None or next_post_at <= now:
                if quote_state.get("daily_posted", 0) >= quote_state.get("daily_quota", 0):
                    next_day = self._start_of_next_day()
                    quote_state["next_post_at"] = next_day.timestamp()
                    self.save_state(self.state)
                else:
                    posted = await self._post_random_quote()
                    if posted:
                        quote_state["daily_posted"] = quote_state.get("daily_posted", 0) + 1
                    quote_state["next_post_at"] = self._schedule_next()
                    self.save_state(self.state)
                    next_post_at = quote_state["next_post_at"]

            sleep_for = max(1, (next_post_at or time.time()) - time.time())
            await asyncio.sleep(sleep_for)

    def _load_quotes(self) -> dict[str, list[str]]:
        quotes: dict[str, list[str]] = {}
        for character in self.characters.keys():
            file_path = self.quotes_dir / f"{character}.txt"
            if not file_path.exists():
                self.log.warning("Missing quotes file for character %s", character)
                continue
            with file_path.open("r", encoding="utf-8") as handle:
                lines = [line.rstrip("\n") for line in handle]
            quotes[character] = lines
        return quotes

    def _ensure_daily_state(self) -> None:
        quote_state = self.state.setdefault("quotes", {})
        today = datetime.now().date().isoformat()
        if quote_state.get("date") != today:
            quote_state["date"] = today
            quote_state["daily_quota"] = random.randint(self.daily_min, self.daily_max)
            quote_state["daily_posted"] = 0
            quote_state["next_post_at"] = self._schedule_next()
            quote_state.setdefault("characters", {})
            self.save_state(self.state)

    def _window_bounds_today(self) -> tuple[datetime, datetime]:
        now = datetime.now()
        if self.window_start and self.window_end:
            start = now.replace(
                hour=self.window_start[0], minute=self.window_start[1], second=0, microsecond=0
            )
            end = now.replace(
                hour=self.window_end[0], minute=self.window_end[1], second=0, microsecond=0
            )
        else:
            start = datetime.combine(now.date(), datetime.min.time())
            end = start + timedelta(days=1)
        return start, end

    def _schedule_next(self) -> float:
        now = datetime.now()
        start, end = self._window_bounds_today()

        # If we're past the window end, schedule for start of next day's window
        if now >= end:
            if self.window_start:
                tomorrow = now.date() + timedelta(days=1)
                next_start = datetime.combine(tomorrow, datetime.min.time()).replace(
                    hour=self.window_start[0], minute=self.window_start[1]
                )
                return next_start.timestamp()
            return (datetime.combine(now.date(), datetime.min.time()) + timedelta(days=1)).timestamp()

        # Clamp to window start if we're before the window
        earliest = max(now, start)
        remaining = (end - earliest).total_seconds()
        if remaining <= 0:
            return time.time() + 60
        offset = random.uniform(0, remaining)
        return (earliest + timedelta(seconds=offset)).timestamp()

    def _start_of_next_day(self) -> datetime:
        now = datetime.now()
        return datetime.combine(now.date(), datetime.min.time()) + timedelta(days=1)

    async def _post_random_quote(self) -> bool:
        # Enforce posting window
        now = datetime.now()
        start, end = self._window_bounds_today()
        if now < start or now >= end:
            self.log.info("Outside posting window (%s-%s), skipping.", start.time(), end.time())
            return False

        quote_state = self.state.setdefault("quotes", {})
        character_state = quote_state.setdefault("characters", {})
        candidates = list(self.quotes.keys())
        random.shuffle(candidates)

        for character in candidates:
            remaining = character_state.setdefault(character, {}).get("remaining_indices")
            if not remaining:
                remaining = list(range(len(self.quotes[character])))
                random.shuffle(remaining)
                character_state[character] = {"remaining_indices": remaining}
            quote = self._next_valid_quote(character, remaining)
            if quote is None:
                continue
            webhook_url = self.characters.get(character)
            if not webhook_url:
                continue
            await self.webhook.send(webhook_url, quote)
            self.save_state(self.state)
            self.log.info("Posted quote for %s", character)
            return True

        self.save_state(self.state)
        self.log.warning("No valid quotes found to post.")
        return False

    def _next_valid_quote(self, character: str, remaining: list[int]) -> str | None:
        quotes = self.quotes.get(character, [])
        while remaining:
            index = remaining.pop()
            quote = quotes[index].strip()
            if not quote:
                continue
            if not self._passes_rules(quote):
                continue
            return quote
        return None

    def _passes_rules(self, quote: str) -> bool:
        if len(quote) > self.max_chars:
            return False
        if self.no_mentions:
            if "@everyone" in quote or "@here" in quote or "<@" in quote or "<@&" in quote:
                return False
        if self.no_links:
            lowered = quote.lower()
            if "http://" in lowered or "https://" in lowered or "www." in lowered:
                return False
        sentences = [s for s in re.split(r"[.!?]", quote) if s.strip()]
        if len(sentences) > self.max_sentences:
            return False
        return True
