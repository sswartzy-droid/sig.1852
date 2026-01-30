import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from discord_webhook import DiscordWebhook

MAX_DAILY_QUOTES = 3


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
        self.daily_max = min(int(quotes_config.get("daily_max", 3)), MAX_DAILY_QUOTES)
        self.max_sentences = int(quotes_config.get("max_sentences", 3))
        self.max_chars = int(quotes_config.get("max_chars", 350))
        self.no_links = bool(quotes_config.get("no_links", True))
        self.no_mentions = bool(quotes_config.get("no_mentions", True))
        self.weights: dict[str, int] = {
            k: int(v) for k, v in quotes_config.get("weights", {}).items()
        }
        self.quotes = self._load_quotes()

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
        files_map = self.quotes_config.get("files", {})
        for character, filename in files_map.items():
            file_path = self.quotes_dir / filename
            if not file_path.exists():
                self.log.warning("Missing quotes file %s for character %s", file_path, character)
                continue
            with file_path.open("r", encoding="utf-8") as handle:
                content = handle.read()
            blocks = [block.strip() for block in re.split(r"\n\s*\n", content)]
            blocks = [b for b in blocks if b]
            if blocks:
                quotes[character] = blocks
                self.log.info("Loaded %d quotes for %s", len(blocks), character)
            else:
                self.log.warning("No quotes found in %s", file_path)
        return quotes

    def _ensure_daily_state(self) -> None:
        quote_state = self.state.setdefault("quotes", {})
        today = datetime.now().date().isoformat()
        if quote_state.get("date") != today:
            quote_state["date"] = today
            quota = random.randint(self.daily_min, self.daily_max)
            quote_state["daily_quota"] = min(quota, MAX_DAILY_QUOTES)
            quote_state["daily_posted"] = 0
            quote_state["next_post_at"] = self._schedule_next()
            quote_state.setdefault("characters", {})
            self.save_state(self.state)

    def _schedule_next(self) -> float:
        now = datetime.now()
        end_of_day = datetime.combine(now.date(), datetime.min.time()) + timedelta(days=1)
        remaining = (end_of_day - now).total_seconds()
        if remaining <= 0:
            return end_of_day.timestamp()
        offset = random.uniform(0, remaining)
        return (now + timedelta(seconds=offset)).timestamp()

    def _start_of_next_day(self) -> datetime:
        now = datetime.now()
        return datetime.combine(now.date(), datetime.min.time()) + timedelta(days=1)

    def _weighted_character_order(self) -> list[str]:
        candidates = list(self.quotes.keys())
        if not candidates:
            return []
        weights = [self.weights.get(c, 1) for c in candidates]
        ordered: list[str] = []
        remaining_candidates = list(candidates)
        remaining_weights = list(weights)
        while remaining_candidates:
            chosen = random.choices(remaining_candidates, weights=remaining_weights, k=1)[0]
            ordered.append(chosen)
            idx = remaining_candidates.index(chosen)
            remaining_candidates.pop(idx)
            remaining_weights.pop(idx)
        return ordered

    async def _post_random_quote(self) -> bool:
        quote_state = self.state.setdefault("quotes", {})
        character_state = quote_state.setdefault("characters", {})

        for character in self._weighted_character_order():
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
