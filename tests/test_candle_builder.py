from __future__ import annotations

import unittest
from decimal import Decimal

from app.data.candle_builder import (
    CandleBuilder,
    CandleSeries,
    OHLCVCandle,
    floor_time_ms,
    interval_to_ms,
    rest_rows_to_series,
)
from app.data.market_events import TradeEvent


class CandleBuilderTests(unittest.TestCase):
    def test_interval_to_ms(self) -> None:
        self.assertEqual(interval_to_ms("1m"), 60_000)
        self.assertEqual(interval_to_ms("30min"), 30 * 60_000)
        self.assertEqual(interval_to_ms("1h"), 3_600_000)
        self.assertEqual(interval_to_ms("2h"), 2 * 3_600_000)
        self.assertEqual(interval_to_ms("24h"), 24 * 3_600_000)
        with self.assertRaises(ValueError):
            interval_to_ms("2m")

    def test_trade_aggregation_closes_previous_candle(self) -> None:
        builder = CandleBuilder(pair="B-BTC_USDT", interval="1m")
        first = TradeEvent(
            pair="B-BTC_USDT",
            price=Decimal("100"),
            quantity=Decimal("2"),
            timestamp_ms=1700000000000,
        )
        second = TradeEvent(
            pair="B-BTC_USDT",
            price=Decimal("110"),
            quantity=Decimal("3"),
            timestamp_ms=1700000005000,
        )
        third = TradeEvent(
            pair="B-BTC_USDT",
            price=Decimal("105"),
            quantity=Decimal("1"),
            timestamp_ms=floor_time_ms(1700000000000, "1m") + 60_000,
        )

        self.assertEqual(builder.update_trade(first), [])
        self.assertEqual(builder.update_trade(second), [])
        closed = builder.update_trade(third)

        self.assertEqual(len(closed), 1)
        candle = closed[0]
        self.assertEqual(candle.open, Decimal("100"))
        self.assertEqual(candle.high, Decimal("110"))
        self.assertEqual(candle.low, Decimal("100"))
        self.assertEqual(candle.close, Decimal("110"))
        self.assertEqual(candle.volume, Decimal("5"))
        self.assertEqual(candle.quote_volume, Decimal("530"))
        self.assertEqual(candle.trade_count, 2)

    def test_rest_rows_to_series_sorts_and_dedupes(self) -> None:
        rows = [
            {
                "time": 1700000060000,
                "open": 2,
                "high": 3,
                "low": 1,
                "close": 2,
                "volume": 10,
            },
            {
                "time": 1700000000000,
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 5,
            },
        ]

        series = rest_rows_to_series(pair="B-BTC_USDT", interval="1m", rows=rows)
        self.assertEqual(len(series), 2)
        self.assertEqual(series[0].open_time_ms, 1700000000000)
        self.assertEqual(series[1].open_time_ms, 1700000060000)

    def test_series_replaces_matching_candle(self) -> None:
        first = OHLCVCandle(
            pair="B-BTC_USDT",
            interval="1m",
            open_time_ms=1,
            close_time_ms=60_000,
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("1"),
        )
        replacement = OHLCVCandle(
            pair="B-BTC_USDT",
            interval="1m",
            open_time_ms=1,
            close_time_ms=60_000,
            open=Decimal("2"),
            high=Decimal("2"),
            low=Decimal("2"),
            close=Decimal("2"),
            volume=Decimal("2"),
        )

        series = CandleSeries([first])
        series.add(replacement)
        self.assertEqual(len(series), 1)
        self.assertEqual(series.latest().close, Decimal("2"))  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
