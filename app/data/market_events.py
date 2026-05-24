from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Literal


@dataclass(frozen=True)
class OrderBookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class TradeEvent:
    pair: str
    price: Decimal
    quantity: Decimal
    timestamp_ms: int
    maker: bool | None = None
    product: str = "futures"
    event_type: Literal["trade"] = "trade"


@dataclass(frozen=True)
class PriceEvent:
    pair: str
    price: Decimal
    timestamp_ms: int
    product: str = "futures"
    event_type: Literal["price"] = "price"


@dataclass(frozen=True)
class CandleEvent:
    pair: str
    interval: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    open_time_ms: int
    close_time_ms: int
    quote_volume: Decimal | None = None
    product: str = "futures"
    is_closed: bool = True
    event_type: Literal["candle"] = "candle"


@dataclass(frozen=True)
class OrderBookSnapshotEvent:
    pair: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    timestamp_ms: int
    version: int | None = None
    product: str = "futures"
    event_type: Literal["orderbook_snapshot"] = "orderbook_snapshot"


@dataclass(frozen=True)
class OrderBookUpdateEvent:
    pair: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    timestamp_ms: int
    version: int | None = None
    product: str = "futures"
    event_type: Literal["orderbook_update"] = "orderbook_update"


@dataclass(frozen=True)
class CurrentPricesEvent:
    prices: dict[str, Decimal]
    timestamp_ms: int
    version: int | None = None
    product: str = "futures"
    event_type: Literal["current_prices"] = "current_prices"


MarketEvent = (
    TradeEvent
    | PriceEvent
    | CandleEvent
    | OrderBookSnapshotEvent
    | OrderBookUpdateEvent
    | CurrentPricesEvent
)


def event_to_dict(event: MarketEvent) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, list):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return value

    return convert(asdict(event))
