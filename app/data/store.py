from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal

from app.data.market_events import (
    CandleEvent,
    CurrentPricesEvent,
    MarketEvent,
    OrderBookLevel,
    OrderBookSnapshotEvent,
    OrderBookUpdateEvent,
    PriceEvent,
    TradeEvent,
)


@dataclass
class InMemoryMarketStore:
    max_trades_per_pair: int = 1000
    max_candles_per_series: int = 1000
    max_price_pairs: int = 500
    max_orderbook_pairs: int = 500
    max_candle_series: int = 500
    trades: dict[str, deque[TradeEvent]] = field(default_factory=dict)
    prices: dict[str, PriceEvent] = field(default_factory=dict)
    candles: dict[tuple[str, str], deque[CandleEvent]] = field(default_factory=dict)
    orderbooks: dict[str, OrderBookSnapshotEvent] = field(default_factory=dict)
    current_prices: CurrentPricesEvent | None = None
    event_counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        limits = {
            "max_trades_per_pair": self.max_trades_per_pair,
            "max_candles_per_series": self.max_candles_per_series,
            "max_price_pairs": self.max_price_pairs,
            "max_orderbook_pairs": self.max_orderbook_pairs,
            "max_candle_series": self.max_candle_series,
        }
        for name, value in limits.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive.")

    def apply(self, event: MarketEvent) -> None:
        self.event_counts[event.event_type] = self.event_counts.get(event.event_type, 0) + 1

        if isinstance(event, TradeEvent):
            self.trades.setdefault(
                event.pair, deque(maxlen=self.max_trades_per_pair)
            ).append(event)
        elif isinstance(event, PriceEvent):
            self.prices[event.pair] = event
            _trim_dict(self.prices, self.max_price_pairs)
        elif isinstance(event, CandleEvent):
            key = (event.pair, event.interval)
            candles = self.candles.setdefault(
                key,
                deque(maxlen=self.max_candles_per_series),
            )
            _append_or_replace_candle(candles, event)
            _trim_dict(self.candles, self.max_candle_series)
        elif isinstance(event, OrderBookSnapshotEvent):
            self.orderbooks[event.pair] = event
            _trim_dict(self.orderbooks, self.max_orderbook_pairs)
        elif isinstance(event, OrderBookUpdateEvent):
            self.orderbooks[event.pair] = _merge_orderbook_update(
                self.orderbooks.get(event.pair),
                event,
            )
            _trim_dict(self.orderbooks, self.max_orderbook_pairs)
        elif isinstance(event, CurrentPricesEvent):
            self.current_prices = event

    def latest_candle(self, pair: str, interval: str) -> CandleEvent | None:
        candles = self.candles.get((pair, interval))
        if not candles:
            return None
        return candles[-1]

    def summary(self) -> dict[str, object]:
        return {
            "event_counts": dict(self.event_counts),
            "pairs_with_trades": sorted(self.trades),
            "latest_prices": sorted(self.prices),
            "latest_candles": [f"{pair}:{interval}" for pair, interval in sorted(self.candles)],
            "latest_orderbooks": sorted(self.orderbooks),
            "has_current_prices": self.current_prices is not None,
        }


def _trim_dict(values: dict, max_items: int) -> None:
    while len(values) > max_items:
        oldest_key = next(iter(values))
        del values[oldest_key]


def _append_or_replace_candle(
    candles: deque[CandleEvent],
    event: CandleEvent,
) -> None:
    for index, existing in enumerate(candles):
        if existing.open_time_ms == event.open_time_ms:
            candles[index] = event
            return
    candles.append(event)


def _merge_orderbook_update(
    existing: OrderBookSnapshotEvent | None,
    update: OrderBookUpdateEvent,
) -> OrderBookSnapshotEvent:
    bids = _levels_to_dict(existing.bids if existing is not None else [])
    asks = _levels_to_dict(existing.asks if existing is not None else [])

    _apply_level_updates(bids, update.bids)
    _apply_level_updates(asks, update.asks)

    return OrderBookSnapshotEvent(
        pair=update.pair,
        bids=[
            OrderBookLevel(price=price, quantity=quantity)
            for price, quantity in sorted(bids.items(), reverse=True)
        ],
        asks=[
            OrderBookLevel(price=price, quantity=quantity)
            for price, quantity in sorted(asks.items())
        ],
        timestamp_ms=update.timestamp_ms,
        version=update.version,
        product=update.product,
    )


def _levels_to_dict(levels: list[OrderBookLevel]) -> dict[Decimal, Decimal]:
    return {level.price: level.quantity for level in levels}


def _apply_level_updates(
    levels: dict[Decimal, Decimal],
    updates: list[OrderBookLevel],
) -> None:
    for update in updates:
        if update.quantity <= 0:
            levels.pop(update.price, None)
        else:
            levels[update.price] = update.quantity
