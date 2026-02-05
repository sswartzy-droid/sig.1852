import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from discord_webhook import DiscordWebhook

MAX_DAILY_QUOTES = 3

QuoteFilter = Callable[[str], bool]


def load_quotes(
    quotes_dir: Path, files_map: dict[str, str], log: logging.Logger
) -> dict[str, list[str]]:
    quotes: dict[str, list[str]] = {}
    for character, filename in files_map.items():
        file_path = quotes_dir / filename
        if not file_path.exists():
            log.warning("Missing quotes file %s for character %s", file_path, character)
            continue
        with file_path.open("r", encoding="utf-8") as handle:
            content = handle.read()
        blocks = [block.strip() for block in re.split(r"\n\s*\n", content)]
        blocks = [b for b in blocks if b]
        if blocks:
            quotes[character] = blocks
            log.info("Loaded %d quotes for %s", len(blocks), character)
        else:
            log.warning("No quotes found in %s", file_path)
    return quotes


def _check_length(max_chars: int) -> QuoteFilter:
    def check(quote: str) -> bool:
        return len(quote) <= max_chars
    return check


def _check_mentions(quote: str) -> bool:
    return not any(
        marker in quote for marker in ("@everyone", "@here", "<@", "<@&")
    )


def _check_links(quote: str) -> bool:
    lowered = quote.lower()
    return not any(
        prefix in lowered for prefix in ("http://", "https://", "www.")
    )


def _check_sentences(max_sentences: int) -> QuoteFilter:
    def check(quote: str) -> bool:
        sentences = [s for s in re.split(r"[.!?]", quote) if s.strip()]
        return len(sentences) <= max_sentences
    return check


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
        self.weights: dict[str, int] = {
            k: int(v) for k, v in quotes_config.get("weights", {}).items()
        }
        self.filters = self._build_filters(quotes_config)
        self.quotes = load_quotes(
            self.quotes_dir, quotes_config.get("files", {}), self.log
        )

    @staticmethod
    def _build_filters(config: dict) -> list[QuoteFilter]:
        filters: list[QuoteFilter] = [
            _check_length(int(config.get("max_chars", 350))),
            _check_sentences(int(config.get("max_sentences", 3))),
        ]
        if config.get("no_mentions", True):
            filters.append(_check_mentions)
        if config.get("no_links", True):
            filters.append(_check_links)
        return filters

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
                elif quote_state.get("_exhausted", False):
                    next_day = self._start_of_next_day()
                    quote_state["next_post_at"] = next_day.timestamp()
                    self.save_state(self.state)
                    self.log.info("All quotes exhausted for today; sleeping until tomorrow.")
                else:
                    posted = await self._post_random_quote()
                    if posted:
                        quote_state["daily_posted"] = quote_state.get("daily_posted", 0) + 1
                    else:
                        quote_state["_exhausted"] = True
                    quote_state["next_post_at"] = self._schedule_next()
                    self.save_state(self.state)
                    next_post_at = quote_state["next_post_at"]

            sleep_for = max(1, (next_post_at or time.time()) - time.time())
            await asyncio.sleep(sleep_for)

    def _ensure_daily_state(self) -> None:
        quote_state = self.state.setdefault("quotes", {})
        today = datetime.now().date().isoformat()
        if quote_state.get("date") != today:
            quote_state["date"] = today
            quota = random.randint(self.daily_min, self.daily_max)
            quote_state["daily_quota"] = min(quota, MAX_DAILY_QUOTES)
            quote_state["daily_posted"] = 0
            quote_state["_exhausted"] = False
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

    def _pick_weighted_character(self) -> list[str]:
        candidates = list(self.quotes.keys())
        if not candidates:
            return []
        weights = [self.weights.get(c, 1) for c in candidates]
        return random.choices(candidates, weights=weights, k=len(candidates))

    async def _post_random_quote(self) -> bool:
        quote_state = self.state.setdefault("quotes", {})
        character_state = quote_state.setdefault("characters", {})

        # Build a deduplicated weighted order
        seen: set[str] = set()
        order: list[str] = []
        for c in self._pick_weighted_character():
            if c not in seen:
                seen.add(c)
                order.append(c)

        for character in order:
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
            if index >= len(quotes):
                continue
            quote = quotes[index].strip()
            if not quote:
                continue
            if not self._passes_filters(quote):
                continue
            return quote
        return None

    def _passes_filters(self, quote: str) -> bool:
        return all(f(quote) for f in self.filters)
