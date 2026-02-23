import asyncio
import logging
import random
from collections import deque
from pathlib import Path
from typing import Any

from quote_drip import (
    QuoteFilter,
    _check_length,
    _check_links,
    _check_mentions,
    _check_sentences,
)


def _build_brb_filters(config: dict[str, Any]) -> list[QuoteFilter]:
    filters: list[QuoteFilter] = [
        _check_length(int(config.get("max_chars", 350))),
        _check_sentences(int(config.get("max_sentences", 3))),
    ]
    if config.get("no_mentions", True):
        filters.append(_check_mentions)
    if config.get("no_links", True):
        filters.append(_check_links)
    return filters


class BrbFeed:
    """Writes quote blocks to intermission.txt on a timer while BRB mode is active.

    Activated and deactivated via start()/stop() from twitch_chat.py in response
    to the !brb and !back commands. Does not run automatically.
    """

    def __init__(
        self,
        brb_config: dict[str, Any],
        quotes: dict[str, list[str]],
        quotes_config: dict[str, Any],
    ) -> None:
        self.output_file = Path(brb_config.get("output_file", "intermission.txt"))
        self.interval = int(brb_config.get("interval_seconds", 15))
        buffer_size = int(brb_config.get("recent_quote_buffer", 50))
        self.log = logging.getLogger("brb.feed")
        self.quotes = quotes
        self.weights: dict[str, int] = {
            k: int(v) for k, v in quotes_config.get("weights", {}).items()
        }
        self.filters = _build_brb_filters(quotes_config)
        self._recent: deque[str] = deque(maxlen=buffer_size)
        self._task: asyncio.Task | None = None
        self._active = False

    async def start(self) -> None:
        """Clear intermission.txt and begin appending quotes on the configured interval."""
        if self._active:
            self.log.warning("BRB feed already running; ignoring start().")
            return
        self._clear_output()
        self._active = True
        self._task = asyncio.create_task(self._loop())
        self.log.info("BRB feed started (interval=%ds, output=%s).", self.interval, self.output_file)

    async def stop(self) -> None:
        """Stop the quote append loop."""
        if not self._active:
            return
        self._active = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self.log.info("BRB feed stopped.")

    @property
    def is_active(self) -> bool:
        return self._active

    def _clear_output(self) -> None:
        try:
            self.output_file.write_text("", encoding="utf-8")
            self.log.debug("Cleared %s for new BRB session.", self.output_file)
        except OSError:
            self.log.exception("Failed to clear %s.", self.output_file)

    async def _loop(self) -> None:
        while self._active:
            await asyncio.sleep(self.interval)
            if not self._active:
                break
            self._append_quote()

    def _append_quote(self) -> None:
        quote = self._pick_quote()
        if quote is None:
            self.log.warning("No valid quote available for BRB feed; skipping cycle.")
            return
        try:
            existing = (
                self.output_file.read_text(encoding="utf-8").splitlines()
                if self.output_file.exists()
                else []
            )
            existing.append(quote)
            if len(existing) > 200:
                existing = existing[-200:]
            self.output_file.write_text("\n".join(existing) + "\n", encoding="utf-8")
            self._recent.append(quote)
            self.log.debug("BRB feed: appended quote (%d chars).", len(quote))
        except OSError:
            self.log.exception("Failed to write to %s.", self.output_file)

    def _pick_quote(self) -> str | None:
        candidates = [c for c in self.quotes if self.quotes[c]]
        if not candidates:
            return None
        weights = [self.weights.get(c, 1) for c in candidates]

        # Up to 50 weighted-random attempts to find a non-recent, valid quote.
        for _ in range(50):
            character = random.choices(candidates, weights=weights, k=1)[0]
            quote = random.choice(self.quotes[character]).strip()
            if not quote or quote in self._recent or not self._passes_filters(quote):
                continue
            return quote

        # Fallback: ignore recency constraint and return any valid quote.
        self.log.debug("BRB recent buffer saturated; falling back to any valid quote.")
        for _ in range(20):
            character = random.choices(candidates, weights=weights, k=1)[0]
            quote = random.choice(self.quotes[character]).strip()
            if quote and self._passes_filters(quote):
                return quote

        return None

    def _passes_filters(self, quote: str) -> bool:
        return all(f(quote) for f in self.filters)
