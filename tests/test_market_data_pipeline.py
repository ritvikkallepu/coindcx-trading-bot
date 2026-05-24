from __future__ import annotations

import logging
import unittest
from decimal import Decimal

from app.data.normalizer import normalize_coindcx_event
from app.data.pipeline import MarketDataPipeline
from app.data.replay import ReplayMarketDataSource
from app.data.store import InMemoryMarketStore
from app.data.market_events import (
    CandleEvent,
    OrderBookSnapshotEvent,
    OrderBookUpdateEvent,
    PriceEvent,
    TradeEvent,
)


class MarketDataPipelineTests(unittest.TestCase):
    def test_normalizes_trade(self) -> None:
        events = normalize_coindcx_event(
            "new-trade",
            {
                "data": {
                    "T": 1705516361108,
                    "p": "43000.5",
                    "q": "0.012",
                    "m": 1,
                    "s": "B-BTC_USDT",
                    "pr": "f",
                }
            },
        )

        self.assertIsInstance(events[0], TradeEvent)
        trade = events[0]
        self.assertEqual(trade.price, Decimal("43000.5"))
        self.assertEqual(trade.quantity, Decimal("0.012"))
        self.assertTrue(trade.maker)

    def test_normalizes_price_with_default_pair(self) -> None:
        events = normalize_coindcx_event(
            "price-change",
            {"data": {"T": 1705516361108, "p": "43001.0", "pr": "f"}},
            default_pair="B-BTC_USDT",
        )

        self.assertIsInstance(events[0], PriceEvent)
        self.assertEqual(events[0].pair, "B-BTC_USDT")

    def test_normalizes_candle_seconds_to_ms(self) -> None:
        events = normalize_coindcx_event(
            "candlestick",
            {
                "data": {
                    "data": [
                        {
                            "open": "42900",
                            "close": "43010",
                            "high": "43100",
                            "low": "42880",
                            "volume": "125.5",
                            "open_time": 1705514400,
                            "close_time": 1705517999.999,
                            "pair": "B-BTC_USDT",
                            "duration": "1h",
                        }
                    ],
                    "i": "1h",
                    "pr": "futures",
                }
            },
        )

        self.assertIsInstance(events[0], CandleEvent)
        candle = events[0]
        self.assertEqual(candle.open_time_ms, 1705514400000)
        self.assertEqual(candle.close_time_ms, 1705517999999)

    def test_normalizes_orderbook_sorting(self) -> None:
        events = normalize_coindcx_event(
            "depth-update",
            {
                "data": {
                    "ts": 1705913767265,
                    "vs": 53727235,
                    "asks": {"43002": "0.5", "43001.5": "0.2"},
                    "bids": {"43000.4": "1.1", "43000.9": "0.7"},
                    "pr": "futures",
                }
            },
            default_pair="B-BTC_USDT",
        )

        self.assertIsInstance(events[0], OrderBookUpdateEvent)
        orderbook = events[0]
        self.assertEqual(orderbook.bids[0].price, Decimal("43000.9"))
        self.assertEqual(orderbook.asks[0].price, Decimal("43001.5"))

    def test_depth_snapshot_alias_still_supported(self) -> None:
        events = normalize_coindcx_event(
            "depth-snapshot",
            {
                "data": {
                    "ts": 1705913767265,
                    "vs": 53727235,
                    "asks": {"43002": "0.5"},
                    "bids": {"43000.9": "0.7"},
                    "pr": "futures",
                }
            },
            default_pair="B-BTC_USDT",
        )

        self.assertIsInstance(events[0], OrderBookSnapshotEvent)

    def test_missing_timestamp_raises_instead_of_epoch_zero(self) -> None:
        with self.assertRaises(ValueError):
            normalize_coindcx_event(
                "price-change",
                {"data": {"p": "43001.0", "pr": "f"}},
                default_pair="B-BTC_USDT",
            )

    def test_callback_exception_does_not_skip_later_replay_events(self) -> None:
        def failing_callback(_event) -> None:
            raise RuntimeError("callback boom")

        logger = logging.getLogger("test.pipeline.callback")
        logger.disabled = True
        pipeline = MarketDataPipeline(
            default_pair="B-BTC_USDT",
            on_event=failing_callback,
            logger=logger,
        )
        count = ReplayMarketDataSource(pair="B-BTC_USDT").run(pipeline)

        self.assertEqual(count, 4)
        self.assertEqual(pipeline.store.summary()["event_counts"]["orderbook_update"], 1)

    def test_unknown_event_is_skipped_without_crashing_pipeline(self) -> None:
        logger = logging.getLogger("test.pipeline.unknown")
        logger.disabled = True
        pipeline = MarketDataPipeline(default_pair="B-BTC_USDT", logger=logger)

        events = pipeline.handle_raw("unknown-event", {"data": {}})

        self.assertEqual(events, [])
        self.assertEqual(pipeline.store.summary()["event_counts"], {})

    def test_store_bounds_latest_dicts_and_keeps_candle_history(self) -> None:
        store = InMemoryMarketStore(
            max_price_pairs=1,
            max_orderbook_pairs=1,
            max_candle_series=1,
            max_candles_per_series=2,
        )
        for index in range(3):
            store.apply(
                CandleEvent(
                    pair="B-BTC_USDT",
                    interval="1m",
                    open=Decimal(index),
                    high=Decimal(index),
                    low=Decimal(index),
                    close=Decimal(index),
                    volume=Decimal("1"),
                    open_time_ms=index * 60_000,
                    close_time_ms=((index + 1) * 60_000) - 1,
                )
            )

        self.assertEqual(len(store.candles[("B-BTC_USDT", "1m")]), 2)
        self.assertEqual(
            store.latest_candle("B-BTC_USDT", "1m").close,  # type: ignore[union-attr]
            Decimal("2"),
        )

    def test_store_replaces_duplicate_candle_updates(self) -> None:
        store = InMemoryMarketStore()
        original = CandleEvent(
            pair="B-BTC_USDT",
            interval="1m",
            open=Decimal("1"),
            high=Decimal("2"),
            low=Decimal("1"),
            close=Decimal("1.5"),
            volume=Decimal("10"),
            open_time_ms=60_000,
            close_time_ms=119_999,
        )
        replacement = CandleEvent(
            pair="B-BTC_USDT",
            interval="1m",
            open=Decimal("1"),
            high=Decimal("3"),
            low=Decimal("1"),
            close=Decimal("2.5"),
            volume=Decimal("20"),
            open_time_ms=60_000,
            close_time_ms=119_999,
        )

        store.apply(original)
        store.apply(replacement)

        self.assertEqual(len(store.candles[("B-BTC_USDT", "1m")]), 1)
        self.assertEqual(store.latest_candle("B-BTC_USDT", "1m").close, Decimal("2.5"))  # type: ignore[union-attr]

    def test_orderbook_updates_merge_and_remove_zero_quantity_levels(self) -> None:
        store = InMemoryMarketStore()
        snapshot = normalize_coindcx_event(
            "depth-snapshot",
            {
                "data": {
                    "ts": 1705913767000,
                    "vs": 1,
                    "asks": {"101": "1", "102": "2"},
                    "bids": {"99": "1", "98": "2"},
                    "pr": "futures",
                }
            },
            default_pair="B-BTC_USDT",
        )[0]
        update = normalize_coindcx_event(
            "depth-update",
            {
                "data": {
                    "ts": 1705913768000,
                    "vs": 2,
                    "asks": {"101": "0", "103": "3"},
                    "bids": {"99": "4", "98": "0"},
                    "pr": "futures",
                }
            },
            default_pair="B-BTC_USDT",
        )[0]

        store.apply(snapshot)
        store.apply(update)
        orderbook = store.orderbooks["B-BTC_USDT"]

        self.assertEqual([level.price for level in orderbook.asks], [Decimal("102"), Decimal("103")])
        self.assertEqual(orderbook.bids[0].price, Decimal("99"))
        self.assertEqual(orderbook.bids[0].quantity, Decimal("4"))
        self.assertEqual(len(orderbook.bids), 1)

    def test_replay_updates_store(self) -> None:
        pipeline = MarketDataPipeline(default_pair="B-BTC_USDT")
        count = ReplayMarketDataSource(pair="B-BTC_USDT").run(pipeline)

        self.assertEqual(count, 4)
        summary = pipeline.store.summary()
        self.assertEqual(summary["event_counts"]["trade"], 1)
        self.assertEqual(summary["event_counts"]["price"], 1)
        self.assertEqual(summary["event_counts"]["candle"], 1)
        self.assertEqual(summary["event_counts"]["orderbook_update"], 1)


if __name__ == "__main__":
    unittest.main()
