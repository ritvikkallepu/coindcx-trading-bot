from __future__ import annotations

import unittest
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.config import Settings
from app.dashboard.state import build_strategy_profile
from app.data.candle_builder import CandleSeries, OHLCVCandle
from app.data.indicators import latest_indicator_snapshot
from app.live.live_loop import _strategy_warmup_lookback
from app.live.paper_loop import PaperTradingLoop
from app.main import build_parser
from app.persistence.paper_state import PaperSessionStore, PaperStateStore
from app.strategies.base import SignalAction, SignalDirection, StrategyContext
from app.strategies.defaults import strategy_engine_for_name
from app.strategies.fib_ma_pullback import (
    FibMAPullbackStrategy,
    _effective_latest_volume,
    _robust_average_volume,
)


def _series(rows: list[tuple[Decimal, Decimal, Decimal, Decimal]]) -> CandleSeries:
    candles = [
        OHLCVCandle(
            pair="B-BTC_USDT",
            interval="1h",
            open_time_ms=index * 3_600_000,
            close_time_ms=((index + 1) * 3_600_000) - 1,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=Decimal("120") if index == len(rows) - 1 else Decimal("100"),
        )
        for index, (open_price, high, low, close) in enumerate(rows)
    ]
    return CandleSeries(candles)


def _evaluate(rows: list[tuple[Decimal, Decimal, Decimal, Decimal]]):
    series = _series(rows)
    return _evaluate_series(series)


def _evaluate_series(series: CandleSeries, features: dict | None = None):
    context = StrategyContext(
        pair="B-BTC_USDT",
        interval="1h",
        candles=series,
        indicators=latest_indicator_snapshot(series),
        features=features or {},
    )
    return FibMAPullbackStrategy(
        fast_period=3,
        slow_period=6,
        atr_period=3,
        volume_period=3,
        swing_lookback=10,
        pivot_left_bars=1,
        pivot_right_bars=1,
        min_swing_atr=Decimal("1.0"),
    ).evaluate(context)


def _exec_candle(
    index: int,
    *,
    open_price: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal = Decimal("100"),
) -> OHLCVCandle:
    base_ms = 10 * 3_600_000
    interval_ms = 5 * 60_000
    return OHLCVCandle(
        pair="B-BTC_USDT",
        interval="5m",
        open_time_ms=base_ms + (index * interval_ms),
        close_time_ms=base_ms + ((index + 1) * interval_ms) - 1,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


class FibMAPullbackTests(unittest.TestCase):
    def test_strategy_is_registered_and_described_for_dashboard(self) -> None:
        engine = strategy_engine_for_name("fib_ma_pullback")
        profile = build_strategy_profile(strategy="fib_ma_pullback", interval="1h")

        self.assertEqual(engine.strategies[0].name, "fib_ma_pullback")
        self.assertEqual(profile["label"], "Fib MA Pullback")
        self.assertEqual(_strategy_warmup_lookback(engine.strategies), 250)

    def test_backtest_parser_accepts_fib_ma_pullback(self) -> None:
        args = build_parser().parse_args(
            ["backtest", "--strategy", "fib_ma_pullback", "--interval", "1h"]
        )

        self.assertEqual(args.strategy, "fib_ma_pullback")

    def test_paper_warmup_honors_fib_ma_history_requirement(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_store = PaperStateStore(f"{temp_dir}/paper.db")
            loop = PaperTradingLoop(
                Settings(),
                strategy_name="fib_ma_pullback",
                state_store=state_store,
                session_store=PaperSessionStore(f"{temp_dir}/session.json"),
            )
            try:
                with patch(
                    "app.backtest.data_loader.load_historical_candle_series",
                    return_value=CandleSeries(),
                ) as load_series:
                    loop._warm_up("B-BTC_USDT", "1h", lookback=50)

                self.assertEqual(load_series.call_args.kwargs["lookback"], 250)
            finally:
                state_store.close()

    def test_enters_long_from_retracement_zone_with_protection(self) -> None:
        signal = _evaluate(
            [
                (Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100")),
                (Decimal("100"), Decimal("100.5"), Decimal("98"), Decimal("99.5")),
                (Decimal("99.5"), Decimal("102"), Decimal("99.5"), Decimal("101")),
                (Decimal("101"), Decimal("104"), Decimal("101"), Decimal("103")),
                (Decimal("103"), Decimal("106"), Decimal("103"), Decimal("105")),
                (Decimal("105"), Decimal("109"), Decimal("105"), Decimal("108")),
                (Decimal("108"), Decimal("112"), Decimal("108"), Decimal("111")),
                (Decimal("111"), Decimal("111"), Decimal("107"), Decimal("108")),
                (Decimal("108"), Decimal("109"), Decimal("105"), Decimal("106")),
                (Decimal("106"), Decimal("108"), Decimal("104.8"), Decimal("107.5")),
            ]
        )

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertEqual(signal.direction, SignalDirection.LONG)
        self.assertLess(signal.stop_loss, signal.entry_price)
        self.assertGreater(signal.take_profit, signal.entry_price)

    def test_enters_short_from_retracement_zone_with_protection(self) -> None:
        signal = _evaluate(
            [
                (Decimal("110"), Decimal("111"), Decimal("109"), Decimal("110")),
                (Decimal("110"), Decimal("112"), Decimal("109.5"), Decimal("111")),
                (Decimal("111"), Decimal("110"), Decimal("108"), Decimal("109")),
                (Decimal("109"), Decimal("108"), Decimal("105"), Decimal("106")),
                (Decimal("106"), Decimal("106"), Decimal("102"), Decimal("103")),
                (Decimal("103"), Decimal("103"), Decimal("99"), Decimal("100")),
                (Decimal("100"), Decimal("100"), Decimal("96"), Decimal("97")),
                (Decimal("97"), Decimal("101"), Decimal("98"), Decimal("100")),
                (Decimal("100"), Decimal("104"), Decimal("100"), Decimal("103")),
                (Decimal("103"), Decimal("103.5"), Decimal("101"), Decimal("101.5")),
            ]
        )

        self.assertEqual(signal.action, SignalAction.ENTER_SHORT)
        self.assertEqual(signal.direction, SignalDirection.SHORT)
        self.assertGreater(signal.stop_loss, signal.entry_price)
        self.assertLess(signal.take_profit, signal.entry_price)

    def test_does_not_chase_after_swing_break(self) -> None:
        signal = _evaluate(
            [
                (Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100")),
                (Decimal("100"), Decimal("100.5"), Decimal("98"), Decimal("99.5")),
                (Decimal("99.5"), Decimal("102"), Decimal("99.5"), Decimal("101")),
                (Decimal("101"), Decimal("104"), Decimal("101"), Decimal("103")),
                (Decimal("103"), Decimal("106"), Decimal("103"), Decimal("105")),
                (Decimal("105"), Decimal("109"), Decimal("105"), Decimal("108")),
                (Decimal("108"), Decimal("112"), Decimal("108"), Decimal("111")),
                (Decimal("111"), Decimal("111"), Decimal("107"), Decimal("108")),
                (Decimal("108"), Decimal("109"), Decimal("105"), Decimal("106")),
                (Decimal("106"), Decimal("112.5"), Decimal("104.8"), Decimal("112")),
            ]
        )

        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertIn("chase", signal.reason.lower())

    def test_volume_baseline_ignores_single_spike_candle(self) -> None:
        baseline = _robust_average_volume(
            [Decimal("100")] * 19 + [Decimal("6000")],
            period=20,
        )

        self.assertEqual(baseline, Decimal("100"))

    def test_partial_parent_volume_is_projected_to_parent_candle(self) -> None:
        effective_volume, projection_factor = _effective_latest_volume(
            Decimal("20"),
            {"parent_completion_ratio": Decimal("0.20")},
        )

        self.assertEqual(effective_volume, Decimal("100"))
        self.assertEqual(projection_factor, Decimal("5"))

    def test_intrabar_long_enters_on_execution_rejection_from_fib_zone(self) -> None:
        parent_series = _series(
            [
                (Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100")),
                (Decimal("100"), Decimal("100.5"), Decimal("98"), Decimal("99.5")),
                (Decimal("99.5"), Decimal("102"), Decimal("99.5"), Decimal("101")),
                (Decimal("101"), Decimal("104"), Decimal("101"), Decimal("103")),
                (Decimal("103"), Decimal("106"), Decimal("103"), Decimal("105")),
                (Decimal("105"), Decimal("109"), Decimal("105"), Decimal("108")),
                (Decimal("108"), Decimal("112"), Decimal("108"), Decimal("111")),
                (Decimal("111"), Decimal("111"), Decimal("107"), Decimal("108")),
                (Decimal("108"), Decimal("109"), Decimal("105"), Decimal("106")),
            ]
        )
        trigger = _exec_candle(
            2,
            open_price=Decimal("106"),
            high=Decimal("108"),
            low=Decimal("104.8"),
            close=Decimal("107.5"),
            volume=Decimal("140"),
        )

        signal = _evaluate_series(
            parent_series,
            {
                "execution_interval": "5m",
                "execution_candles": [
                    _exec_candle(0, open_price=Decimal("106.5"), high=Decimal("107"), low=Decimal("105.8"), close=Decimal("106"), volume=Decimal("100")),
                    _exec_candle(1, open_price=Decimal("106"), high=Decimal("106.2"), low=Decimal("105"), close=Decimal("105.5"), volume=Decimal("100")),
                    trigger,
                ],
            },
        )

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertEqual(signal.direction, SignalDirection.LONG)
        self.assertEqual(signal.entry_price, trigger.close)
        self.assertEqual(signal.timestamp_ms, trigger.close_time_ms)
        self.assertEqual(signal.metadata["entry_type"], "fib_ma_intrabar_pullback")
        self.assertTrue(signal.metadata["fib_intrabar_trigger"])

    def test_intrabar_long_rejects_weak_red_execution_touch(self) -> None:
        parent_series = _series(
            [
                (Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100")),
                (Decimal("100"), Decimal("100.5"), Decimal("98"), Decimal("99.5")),
                (Decimal("99.5"), Decimal("102"), Decimal("99.5"), Decimal("101")),
                (Decimal("101"), Decimal("104"), Decimal("101"), Decimal("103")),
                (Decimal("103"), Decimal("106"), Decimal("103"), Decimal("105")),
                (Decimal("105"), Decimal("109"), Decimal("105"), Decimal("108")),
                (Decimal("108"), Decimal("112"), Decimal("108"), Decimal("111")),
                (Decimal("111"), Decimal("111"), Decimal("107"), Decimal("108")),
                (Decimal("108"), Decimal("109"), Decimal("105"), Decimal("106")),
            ]
        )

        signal = _evaluate_series(
            parent_series,
            {
                "execution_interval": "5m",
                "execution_candles": [
                    _exec_candle(0, open_price=Decimal("106.5"), high=Decimal("107"), low=Decimal("105.8"), close=Decimal("106"), volume=Decimal("100")),
                    _exec_candle(1, open_price=Decimal("106"), high=Decimal("106.2"), low=Decimal("105"), close=Decimal("105.8"), volume=Decimal("100")),
                    _exec_candle(2, open_price=Decimal("106"), high=Decimal("106.1"), low=Decimal("104.8"), close=Decimal("105.2"), volume=Decimal("140")),
                ],
            },
        )

        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertIn("No intrabar bullish rejection", signal.reason)

    def test_one_minute_execution_does_not_enable_fib_intrabar_entries(self) -> None:
        parent_series = _series(
            [
                (Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100")),
                (Decimal("100"), Decimal("100.5"), Decimal("98"), Decimal("99.5")),
                (Decimal("99.5"), Decimal("102"), Decimal("99.5"), Decimal("101")),
                (Decimal("101"), Decimal("104"), Decimal("101"), Decimal("103")),
                (Decimal("103"), Decimal("106"), Decimal("103"), Decimal("105")),
                (Decimal("105"), Decimal("109"), Decimal("105"), Decimal("108")),
                (Decimal("108"), Decimal("112"), Decimal("108"), Decimal("111")),
                (Decimal("111"), Decimal("111"), Decimal("107"), Decimal("108")),
                (Decimal("108"), Decimal("109"), Decimal("105"), Decimal("106")),
            ]
        )

        signal = _evaluate_series(
            parent_series,
            {
                "execution_interval": "1m",
                "execution_candles": [
                    _exec_candle(0, open_price=Decimal("106"), high=Decimal("108"), low=Decimal("104.8"), close=Decimal("107.5"), volume=Decimal("140")),
                ],
            },
        )

        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertFalse(signal.metadata.get("fib_intrabar_trigger_enabled", False))


if __name__ == "__main__":
    unittest.main()
