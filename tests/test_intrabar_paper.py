from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock
from dataclasses import replace
from app.config import Settings, RiskSettings
from app.data.candle_builder import OHLCVCandle, CandleSeries
from app.live.paper_loop import PaperTradingLoop
from app.strategies.base import SignalAction, SignalDirection, StrategySignal, StrategyContext


def _candle(interval: str, open_time_ms: int, close: Decimal = Decimal("100")) -> OHLCVCandle:
    from app.data.candle_builder import interval_to_ms
    interval_ms = interval_to_ms(interval)
    return OHLCVCandle(
        pair="B-BTC_USDT",
        interval=interval,
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + interval_ms - 1,
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=Decimal("100"),
    )


class IntrabarPaperLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            paper_starting_equity=Decimal("10000"),
            paper_intrabar_enabled=True,
            strategy_interval="15m",
            execution_interval="1m",
            use_partial_parent_candle=False,
            max_entries_per_parent_candle=1,
            risk=RiskSettings(max_risk_per_trade_pct=Decimal("1"))
        )
        self.loop = PaperTradingLoop(self.settings, strategy_name="adaptive_hybrid")
        self.loop.client.get_candles = MagicMock(return_value={"data": []})
        self.loop.gap_guard = MagicMock()
        self.loop.gap_guard.check.return_value.has_gap = False
        self.loop._current_interval = "15m"

        # Mock strategy engine to return an entry signal on demand
        self.loop.strategy_engine.evaluate = MagicMock(return_value=[])

    def test_intrabar_entry_inside_parent_candle(self) -> None:
        # 1. Warm up strategy series (15m) with 10 candles
        self.loop.series = CandleSeries()
        for i in range(10):
            self.loop.series.add(_candle("15m", i * 900000))
        
        # 2. Setup execution series (1m)
        self.loop.execution_series = CandleSeries()
        for i in range(150): # up to 10th 15m candle
            self.loop.execution_series.add(_candle("1m", i * 60000))

        self.loop._current_parent_open_ms = 9 * 900000 # 10th candle opens at 9*15m
        self.loop._entries_this_parent_candle = 0

        # 3. Simulate an execution candle at minute 2 of the 11th parent (10 * 900000 + 120000)
        # Parent 11 opens at 9,000,000 ms
        parent_start = 10 * 900000 
        exec_candle = _candle("1m", parent_start + 120000, Decimal("105"))
        
        # Strategy should see the LAST CLOSED parent (the 10th one)
        # Mock strategy to return entry
        signal = StrategySignal(
            strategy_name="S", pair="B-BTC_USDT", interval="15m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="test", timestamp_ms=parent_start - 1,
            entry_price=Decimal("105"), stop_loss=Decimal("100")
        )
        self.loop.strategy_engine.evaluate.return_value = [signal]

        # 4. Trigger _on_candle
        self.loop._on_candle(exec_candle)

        # 5. Check results
        self.assertEqual(len(self.loop.broker.positions), 1)
        self.assertEqual(self.loop._entries_this_parent_candle, 1)
        self.assertEqual(self.loop.candle_count, 1)

    def test_intrabar_entry_limit_per_parent(self) -> None:
        # Setup as above
        self.loop.series = CandleSeries()
        self.loop.series.add(_candle("15m", 0))
        self.loop.execution_series = CandleSeries()
        self.loop.execution_series.add(_candle("1m", 0))
        self.loop._current_parent_open_ms = 0
        self.loop._entries_this_parent_candle = 1 # Already entered once

        exec_candle = _candle("1m", 60000, Decimal("105"))
        
        signal = StrategySignal(
            strategy_name="S", pair="B-BTC_USDT", interval="15m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="test", timestamp_ms=59999,
            entry_price=Decimal("105"), stop_loss=Decimal("100")
        )
        self.loop.strategy_engine.evaluate.return_value = [signal]

        self.loop._on_candle(exec_candle)

        # Still only 1 entry because limit was reached
        self.assertEqual(self.loop._entries_this_parent_candle, 1)
        self.assertEqual(len(self.loop.broker.positions), 0) # execute_decision not called for new

    def test_parent_candle_change_resets_counter(self) -> None:
        self.loop.series = CandleSeries()
        self.loop.series.add(_candle("15m", 0))
        self.loop.execution_series = CandleSeries()
        self.loop.execution_series.add(_candle("1m", 0))
        self.loop._current_parent_open_ms = 0
        self.loop._entries_this_parent_candle = 5 

        # New parent opens at 900,000 ms
        new_parent_candle = _candle("15m", 900000)
        self.loop._on_candle(new_parent_candle)

        self.assertEqual(self.loop._entries_this_parent_candle, 0)
        self.assertEqual(self.loop._current_parent_open_ms, 900000)

    def test_partial_parent_candle_aggregation(self) -> None:
        self.loop.settings = replace(self.loop.settings, use_partial_parent_candle=True)
        self.loop.series = CandleSeries()
        self.loop.series.add(_candle("15m", 0, Decimal("100"))) # Confirmed closed
        
        self.loop.execution_series = CandleSeries()
        # Current parent starts at 900,000. 1m candles:
        self.loop.execution_series.add(_candle("1m", 900000, Decimal("101")))
        self.loop.execution_series.add(_candle("1m", 960000, Decimal("102")))
        
        provisional_series = self.loop._build_provisional_htf_series()
        
        self.assertEqual(len(provisional_series), 2)
        provisional = provisional_series.latest()
        self.assertEqual(provisional.open, Decimal("101"))
        self.assertEqual(provisional.close, Decimal("102"))
        self.assertEqual(provisional.high, Decimal("103")) # close + 1 in helper
        self.assertEqual(provisional.interval, "15m")
        self.assertEqual(provisional.open_time_ms, 900000)


if __name__ == "__main__":
    unittest.main()
