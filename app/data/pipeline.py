from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.data.market_events import MarketEvent
from app.data.normalizer import normalize_coindcx_event
from app.data.store import InMemoryMarketStore


class MarketDataPipeline:
    def __init__(
        self,
        *,
        default_pair: str | None = None,
        store: InMemoryMarketStore | None = None,
        on_event: Callable[[MarketEvent], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.default_pair = default_pair
        self.store = store or InMemoryMarketStore()
        self.on_event = on_event
        self.logger = logger or logging.getLogger(__name__)

    def handle_raw(self, event_name: str, payload: Any) -> list[MarketEvent]:
        try:
            events = normalize_coindcx_event(
                event_name,
                payload,
                default_pair=self.default_pair,
            )
        except ValueError:
            self.logger.warning(
                "Skipping unsupported or malformed CoinDCX market event %s.",
                event_name,
                exc_info=True,
            )
            return []
        for event in events:
            self.store.apply(event)
            if self.on_event is not None:
                try:
                    self.on_event(event)
                except Exception:
                    self.logger.exception(
                        "Market data event callback failed for %s; continuing.",
                        event.event_type,
                    )
        return events
