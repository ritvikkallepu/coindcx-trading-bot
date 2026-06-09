from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from app.data.market_events import CandleEvent, TradeEvent


FIXED_INTERVAL_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "30min": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "2hr": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
    "24h": 24 * 60 * 60_000,
    "3d": 3 * 24 * 60 * 60_000,
    "1w": 7 * 24 * 60 * 60_000,
    "1M": 30 * 24 * 60 * 60_000,
}


def interval_to_ms(interval: str) -> int:
    try:
        return FIXED_INTERVAL_MS[interval]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported fixed candle interval {interval}. "
            f"Supported intervals: {sorted(FIXED_INTERVAL_MS)}"
        ) from exc


def floor_time_ms(timestamp_ms: int, interval: str) -> int:
    interval_ms = interval_to_ms(interval)
    return (timestamp_ms // interval_ms) * interval_ms


@dataclass(frozen=True)
class OHLCVCandle:
    pair: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal = Decimal("0")
    trade_count: int = 0
    is_closed: bool = True

    @classmethod
    def from_candle_event(cls, event: CandleEvent) -> OHLCVCandle:
        return cls(
            pair=event.pair,
            interval=event.interval,
            open_time_ms=event.open_time_ms,
            close_time_ms=event.close_time_ms,
            open=event.open,
            high=event.high,
            low=event.low,
            close=event.close,
            volume=event.volume,
            quote_volume=event.quote_volume or Decimal("0"),
            trade_count=0,
            is_closed=event.is_closed,
        )

    @classmethod
    def from_rest_row(
        cls,
        *,
        pair: str,
        interval: str,
        row: dict[str, Any],
    ) -> OHLCVCandle:
        open_time_ms = _timestamp_to_ms(row["time"])
        interval_ms = interval_to_ms(interval)
        close_time_ms = open_time_ms + interval_ms - 1
        
        # A candle is only closed if its end time has passed
        import time
        now_ms = int(time.time() * 1000)
        is_closed = now_ms >= close_time_ms
        
        return cls(
            pair=pair,
            interval=interval,
            open_time_ms=open_time_ms,
            close_time_ms=close_time_ms,
            open=_decimal(row["open"]),
            high=_decimal(row["high"]),
            low=_decimal(row["low"]),
            close=_decimal(row["close"]),
            volume=_decimal(row["volume"]),
            quote_volume=_decimal(row.get("quote_volume", "0")),
            trade_count=int(row.get("trade_count", 0) or 0),
            is_closed=is_closed,
        )

    def to_candle_event(self) -> CandleEvent:
        return CandleEvent(
            pair=self.pair,
            interval=self.interval,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            open_time_ms=self.open_time_ms,
            close_time_ms=self.close_time_ms,
            quote_volume=self.quote_volume,
        )

    @property
    def typical_price(self) -> Decimal:
        return (self.high + self.low + self.close) / Decimal("3")


@dataclass
class _WorkingCandle:
    pair: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int

    @classmethod
    def start(cls, trade: TradeEvent, interval: str) -> _WorkingCandle:
        open_time_ms = floor_time_ms(trade.timestamp_ms, interval)
        return cls(
            pair=trade.pair,
            interval=interval,
            open_time_ms=open_time_ms,
            close_time_ms=open_time_ms + interval_to_ms(interval) - 1,
            open=trade.price,
            high=trade.price,
            low=trade.price,
            close=trade.price,
            volume=trade.quantity,
            quote_volume=trade.price * trade.quantity,
            trade_count=1,
        )

    def apply_trade(self, trade: TradeEvent) -> None:
        self.high = max(self.high, trade.price)
        self.low = min(self.low, trade.price)
        self.close = trade.price
        self.volume += trade.quantity
        self.quote_volume += trade.price * trade.quantity
        self.trade_count += 1

    def freeze(self) -> OHLCVCandle:
        return OHLCVCandle(
            pair=self.pair,
            interval=self.interval,
            open_time_ms=self.open_time_ms,
            close_time_ms=self.close_time_ms,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            quote_volume=self.quote_volume,
            trade_count=self.trade_count,
        )


class CandleBuilder:
    def __init__(self, *, pair: str, interval: str) -> None:
        self.pair = pair
        self.interval = interval
        interval_to_ms(interval)
        self._current: _WorkingCandle | None = None

    @property
    def current(self) -> OHLCVCandle | None:
        if self._current is None:
            return None
        return self._current.freeze()

    def update_trade(self, trade: TradeEvent) -> list[OHLCVCandle]:
        if trade.pair != self.pair:
            return []

        closed: list[OHLCVCandle] = []
        trade_bucket = floor_time_ms(trade.timestamp_ms, self.interval)

        if self._current is None:
            self._current = _WorkingCandle.start(trade, self.interval)
            return closed

        if trade_bucket == self._current.open_time_ms:
            self._current.apply_trade(trade)
            return closed

        if trade_bucket < self._current.open_time_ms:
            return closed

        closed.append(self._current.freeze())
        self._current = _WorkingCandle.start(trade, self.interval)
        return closed

    def close_current(self) -> OHLCVCandle | None:
        if self._current is None:
            return None
        closed = self._current.freeze()
        self._current = None
        return closed


class CandleSeries:
    def __init__(self, candles: Iterable[OHLCVCandle] | None = None, *, maxlen: int = 1000):
        if maxlen <= 0:
            raise ValueError("maxlen must be positive.")
        self.maxlen = maxlen
        self._candles: list[OHLCVCandle] = []
        if candles:
            self.extend(candles)

    def __len__(self) -> int:
        return len(self._candles)

    def __iter__(self):
        return iter(self._candles)

    def __getitem__(self, index: int) -> OHLCVCandle:
        return self._candles[index]

    def add(self, candle: OHLCVCandle) -> None:
        replaced = False
        for index, existing in enumerate(self._candles):
            if (
                existing.pair == candle.pair
                and existing.interval == candle.interval
                and existing.open_time_ms == candle.open_time_ms
            ):
                self._candles[index] = candle
                replaced = True
                break

        if not replaced:
            self._candles.append(candle)

        self._candles.sort(key=lambda item: item.open_time_ms)
        if len(self._candles) > self.maxlen:
            self._candles = self._candles[-self.maxlen :]

    def extend(self, candles: Iterable[OHLCVCandle]) -> None:
        for candle in candles:
            self.add(candle)

    def latest(self) -> OHLCVCandle | None:
        if not self._candles:
            return None
        return self._candles[-1]

    def tail(self, count: int) -> list[OHLCVCandle]:
        if count <= 0:
            return []
        return self._candles[-count:]

    def copy(self) -> CandleSeries:
        return CandleSeries(list(self), maxlen=self.maxlen)

    def closes(self) -> list[Decimal]:
        return [candle.close for candle in self._candles]

    def highs(self) -> list[Decimal]:
        return [candle.high for candle in self._candles]

    def lows(self) -> list[Decimal]:
        return [candle.low for candle in self._candles]

    def volumes(self) -> list[Decimal]:
        return [candle.volume for candle in self._candles]

    def apply_trade(self, trade: TradeEvent) -> None:
        """
        Updates the latest candle in the series with a new trade tick.
        Used for intrabar softening and indicator updates between closed candles.
        """
        if not self._candles:
            return
            
        latest = self._candles[-1]
        interval_ms = interval_to_ms(latest.interval)
        trade_bucket = (trade.timestamp_ms // interval_ms) * interval_ms
        
        # Only update if the trade belongs to the current latest candle's time bucket
        if trade_bucket == latest.open_time_ms:
            # OHLCVCandle is frozen, so we must create a new instance
            from dataclasses import replace
            new_candle = replace(
                latest,
                high=max(latest.high, trade.price),
                low=min(latest.low, trade.price),
                close=trade.price,
                volume=latest.volume + trade.quantity,
                quote_volume=latest.quote_volume + (trade.price * trade.quantity),
                trade_count=latest.trade_count + 1,
                is_closed=False # It's now definitely a partial/dirty candle
            )
            self._candles[-1] = new_candle


def rest_rows_to_series(
    *,
    pair: str,
    interval: str,
    rows: Iterable[dict[str, Any]],
    maxlen: int = 1000,
) -> CandleSeries:
    candles = [
        OHLCVCandle.from_rest_row(pair=pair, interval=interval, row=row)
        for row in rows
    ]
    return CandleSeries(candles, maxlen=maxlen)


def candle_events_to_series(
    events: Iterable[CandleEvent],
    *,
    maxlen: int = 1000,
) -> CandleSeries:
    return CandleSeries((OHLCVCandle.from_candle_event(event) for event in events), maxlen=maxlen)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _timestamp_to_ms(value: Any) -> int:
    timestamp = Decimal(str(value))
    if timestamp < Decimal("100000000000"):
        timestamp *= Decimal("1000")
    return int(timestamp)
