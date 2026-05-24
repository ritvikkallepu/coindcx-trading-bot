from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Mapping

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


def normalize_coindcx_event(
    event_name: str,
    payload: Any,
    *,
    default_pair: str | None = None,
) -> list[MarketEvent]:
    data = _extract_data(payload)

    if event_name == "new-trade":
        return [_normalize_trade(data, default_pair)]
    if event_name == "price-change":
        return [_normalize_price(data, default_pair)]
    if event_name == "candlestick":
        return _normalize_candles(data, default_pair)
    if event_name == "depth-snapshot":
        return [_normalize_orderbook_snapshot(data, default_pair)]
    if event_name == "depth-update":
        return [_normalize_orderbook_update(data, default_pair)]
    if event_name == "currentPrices@futures#update":
        return [_normalize_current_prices(data)]

    raise ValueError(f"Unsupported CoinDCX market event: {event_name}")


def _extract_data(payload: Any) -> Any:
    if isinstance(payload, str):
        payload = json.loads(payload)

    if isinstance(payload, Mapping) and "data" in payload:
        data = payload["data"]
        if isinstance(data, str):
            return json.loads(data)
        return data

    return payload


def _normalize_trade(data: Any, default_pair: str | None) -> TradeEvent:
    if not isinstance(data, Mapping):
        raise ValueError("Trade payload must be an object.")
    pair = _pair_from(data, default_pair)
    return TradeEvent(
        pair=pair,
        price=_decimal(data["p"]),
        quantity=_decimal(data["q"]),
        timestamp_ms=_timestamp_ms(data.get("T"), "trade timestamp T"),
        maker=_bool_or_none(data.get("m")),
        product=_product(data.get("pr")),
    )


def _normalize_price(data: Any, default_pair: str | None) -> PriceEvent:
    if not isinstance(data, Mapping):
        raise ValueError("Price payload must be an object.")
    return PriceEvent(
        pair=_pair_from(data, default_pair),
        price=_decimal(data["p"]),
        timestamp_ms=_timestamp_ms(data.get("T"), "price timestamp T"),
        product=_product(data.get("pr")),
    )


def _normalize_candles(data: Any, default_pair: str | None) -> list[CandleEvent]:
    if isinstance(data, Mapping):
        candle_rows = data.get("data", data)
        event_interval = data.get("i")
        product = _product(data.get("pr"))
    else:
        candle_rows = data
        event_interval = None
        product = "futures"

    if isinstance(candle_rows, Mapping):
        candle_rows = [candle_rows]
    if not isinstance(candle_rows, list):
        raise ValueError("Candlestick payload must contain a candle object or list.")

    events: list[CandleEvent] = []
    for row in candle_rows:
        if not isinstance(row, Mapping):
            raise ValueError("Candlestick row must be an object.")
        interval = str(row.get("duration") or event_interval or "")
        if not interval:
            raise ValueError("Candlestick interval is missing.")
        events.append(
            CandleEvent(
                pair=_pair_from(row, default_pair),
                interval=interval,
                open=_decimal(row["open"]),
                high=_decimal(row["high"]),
                low=_decimal(row["low"]),
                close=_decimal(row["close"]),
                volume=_decimal(row["volume"]),
                open_time_ms=_timestamp_ms(row.get("open_time"), "candle open_time"),
                close_time_ms=_timestamp_ms(row.get("close_time"), "candle close_time"),
                quote_volume=_optional_decimal(row.get("quote_volume")),
                product=product,
                is_closed=_bool_or_true(row.get("m")),
            )
        )
    return events


def _normalize_orderbook_snapshot(
    data: Any,
    default_pair: str | None,
) -> OrderBookSnapshotEvent:
    pair, bids, asks, timestamp_ms, version, product = _normalize_orderbook_parts(
        data,
        default_pair,
    )
    return OrderBookSnapshotEvent(
        pair=pair,
        bids=bids,
        asks=asks,
        timestamp_ms=timestamp_ms,
        version=version,
        product=product,
    )


def _normalize_orderbook_update(
    data: Any,
    default_pair: str | None,
) -> OrderBookUpdateEvent:
    pair, bids, asks, timestamp_ms, version, product = _normalize_orderbook_parts(
        data,
        default_pair,
    )
    return OrderBookUpdateEvent(
        pair=pair,
        bids=bids,
        asks=asks,
        timestamp_ms=timestamp_ms,
        version=version,
        product=product,
    )


def _normalize_orderbook_parts(
    data: Any,
    default_pair: str | None,
) -> tuple[str, list[OrderBookLevel], list[OrderBookLevel], int, int | None, str]:
    if not isinstance(data, Mapping):
        raise ValueError("Orderbook payload must be an object.")

    bids = [
        OrderBookLevel(price=_decimal(price), quantity=_decimal(quantity))
        for price, quantity in dict(data.get("bids", {})).items()
    ]
    asks = [
        OrderBookLevel(price=_decimal(price), quantity=_decimal(quantity))
        for price, quantity in dict(data.get("asks", {})).items()
    ]

    bids.sort(key=lambda level: level.price, reverse=True)
    asks.sort(key=lambda level: level.price)

    return (
        _pair_from(data, default_pair),
        bids,
        asks,
        _timestamp_ms(data.get("ts") or data.get("E"), "orderbook ts/E"),
        _optional_int(data.get("vs")),
        _product(data.get("pr")),
    )


def _normalize_current_prices(data: Any) -> CurrentPricesEvent:
    if not isinstance(data, Mapping):
        raise ValueError("Current-prices payload must be an object.")
    prices: dict[str, Decimal] = {}
    for pair, values in dict(data.get("prices", {})).items():
        if isinstance(values, Mapping) and values.get("mp") is not None:
            prices[str(pair)] = _decimal(values["mp"])

    return CurrentPricesEvent(
        prices=prices,
        timestamp_ms=_timestamp_ms(data.get("ts"), "current prices ts"),
        version=_optional_int(data.get("vs")),
        product=_product(data.get("pr")),
    )


def _pair_from(data: Mapping[str, Any], default_pair: str | None) -> str:
    pair = data.get("s") or data.get("pair")
    if pair:
        return str(pair)

    channel = data.get("channel")
    if isinstance(channel, str) and channel.startswith("B-"):
        return _pair_from_channel(channel)

    if default_pair:
        return default_pair
    raise ValueError("Could not infer pair from payload.")


def _pair_from_channel(channel: str) -> str:
    if "@" in channel:
        return channel.split("@", 1)[0]

    channel = channel.removesuffix("-futures")
    if "_" in channel:
        return channel.rsplit("_", 1)[0]
    return channel


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(float(str(value)))


def _timestamp_ms(value: Any, field_name: str = "timestamp") -> int:
    if value is None:
        raise ValueError(f"Missing required CoinDCX {field_name}.")
    as_decimal = Decimal(str(value))
    if as_decimal < Decimal("100000000000"):
        as_decimal *= Decimal("1000")
    return int(as_decimal)


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return bool(int(value))


def _product(value: Any) -> str:
    if value in {"f", "futures"}:
        return "futures"
    return str(value or "futures")


def _bool_or_true(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return bool(int(value))
