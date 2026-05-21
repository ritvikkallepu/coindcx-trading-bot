from __future__ import annotations

import unittest
from decimal import Decimal
from dataclasses import replace

from app.backtest.engine import BacktestEngine
from app.backtest.models import BacktestConfig
from app.data.candle_builder import OHLCVCandle
from app.risk.manager import RiskManager
from app.strategies.base import Strategy, StrategyEngine, StrategySignal, SignalAction, SignalDirection


class ATRTrailingTests(unittest.TestCase):
    def test_expanding_atr_does_not_loosen_trailing_stop(self) -> None:
        # Starting equity 1000.
        config = BacktestConfig(
            pair="BTC",
            interval="1h",
            starting_equity=Decimal("1000"),
            leverage=Decimal("1"),
            atr_dynamic_exits_enabled=True,
            atr_stop_enabled=True,
            atr_trailing_enabled=True,
            atr_trailing_multiple=Decimal("2.0"),
            atr_period=1,
        )
        
        class LongEntryStrategy(Strategy):
            name = "long_entry"
            def evaluate(self, context):
                if context.candles[-1].open_time_ms == 0:
                    return StrategySignal(
                        strategy_name=self.name,
                        pair=context.pair,
                        interval=context.interval,
                        action=SignalAction.ENTER_LONG,
                        direction=SignalDirection.LONG,
                        confidence=Decimal("1"),
                        reason="Entry",
                        timestamp_ms=context.candles[-1].close_time_ms,
                        entry_price=Decimal("100"),
                        stop_loss=Decimal("90"),
                    )
                return StrategySignal.hold(
                    strategy_name=self.name,
                    pair=context.pair,
                    interval=context.interval,
                    timestamp_ms=context.candles[-1].close_time_ms,
                    reason="Hold",
                )

        engine = BacktestEngine(
            config=config,
            strategy_engine=StrategyEngine([LongEntryStrategy()]),
            risk_manager=RiskManager(),
        )
        
        # Candle 1: Entry 100. ATR 5. Stop 100 - (5*2) = 90.
        # Candle 2: Price 110. ATR 10. Trailing stop based on price 110.
        # If no cap: distance = 10 * 2 = 20. Stop = 110 - 20 = 90. (No change)
        # If capped at entry ATR (5): distance = 5 * 2 = 10. Stop = 110 - 10 = 100. (Moved up)
        # Candle 3: Price 115. ATR 20. 
        # If no cap: distance = 20 * 2 = 40. Stop = 115 - 40 = 75. (Loosened! Logically wrong)
        # If capped at entry ATR (5): distance = 10. Stop = 115 - 10 = 105. (Moved up)
        
        candles = [
            OHLCVCandle("BTC", "1h", 0, 3599999, Decimal("100"), Decimal("105"), Decimal("95"), Decimal("100"), Decimal("1")),
            OHLCVCandle("BTC", "1h", 3600000, 7199999, Decimal("100"), Decimal("110"), Decimal("100"), Decimal("110"), Decimal("1")),
            OHLCVCandle("BTC", "1h", 7200000, 10799999, Decimal("110"), Decimal("115"), Decimal("110"), Decimal("115"), Decimal("1")),
        ]
        
        result = engine.run(candles)
        self.assertEqual(len(result.trades), 1)
        final_trade = result.trades[0]
        
        # We'll check the metadata for the forced close.
        final_stop = final_trade.metadata.get("atr_stop_loss")
        # With entry ATR cap (5), stop should be at least 105 at price 115.
        self.assertGreaterEqual(final_stop, Decimal("100"))
        # If it loosened, it would be around 75.
        self.assertGreater(final_stop, Decimal("80"))

    def test_manual_mode_uses_atr_trailing_multiple(self) -> None:
        config = BacktestConfig(
            pair="BTC",
            interval="1h",
            starting_equity=Decimal("1000"),
            leverage=Decimal("1"),
            atr_dynamic_exits_enabled=True,
            atr_stop_enabled=True,
            atr_trailing_enabled=True,
            atr_trailing_multiple=Decimal("5.0"), # Different from stop multiple
            atr_stop_multiple=Decimal("2.0"),
            atr_policy_mode="manual",
            atr_period=1,
        )
        
        class LongEntryStrategy(Strategy):
            name = "long_entry"
            def evaluate(self, context):
                if context.candles[-1].open_time_ms == 0:
                    return StrategySignal(
                        strategy_name=self.name,
                        pair=context.pair,
                        interval=context.interval,
                        action=SignalAction.ENTER_LONG,
                        direction=SignalDirection.LONG,
                        confidence=Decimal("1"),
                        reason="Entry",
                        timestamp_ms=context.candles[-1].close_time_ms,
                        entry_price=Decimal("100"),
                        stop_loss=Decimal("80"), # Overridden by ATR
                    )
                return StrategySignal.hold(
                    strategy_name=self.name,
                    pair=context.pair,
                    interval=context.interval,
                    timestamp_ms=context.candles[-1].close_time_ms,
                    reason="Hold",
                )

        engine = BacktestEngine(
            config=config,
            strategy_engine=StrategyEngine([LongEntryStrategy()]),
            risk_manager=RiskManager(),
        )
        
        # Candle 1: Entry 100. ATR 2. Initial stop 100 - (2*2) = 96.
        # Candle 2: Price 110. ATR 2. Trailing stop distance = 2 * 5 = 10.
        # New Stop = 110 - 10 = 100.
        
        candles = [
            OHLCVCandle("BTC", "1h", 0, 3599999, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1")),
            OHLCVCandle("BTC", "1h", 3600000, 7199999, Decimal("100"), Decimal("110"), Decimal("109"), Decimal("110"), Decimal("1")),
        ]
        
        result = engine.run(candles)
        final_trade = result.trades[0]
        final_stop = final_trade.metadata.get("atr_stop_loss")
        self.assertEqual(final_stop, Decimal("100"))

    def test_trailing_mutual_exclusivity(self) -> None:
        # If ATR trailing is enabled, fixed pct trailing should NOT move the stop.
        config = BacktestConfig(
            pair="BTC",
            interval="1h",
            starting_equity=Decimal("1000"),
            leverage=Decimal("1"),
            trailing_stop_enabled=True,
            trailing_stop_activation_pct=Decimal("1"),
            trailing_stop_distance_pct=Decimal("5"), # 100 -> 95
            atr_dynamic_exits_enabled=True,
            atr_trailing_enabled=True,
            atr_trailing_multiple=Decimal("0.5"), # 100 -> 110, ATR 2 -> 110 - 1 = 109
            atr_policy_mode="manual",
            atr_period=1,
        )
        
        class LongEntryStrategy(Strategy):
            name = "long_entry"
            def evaluate(self, context):
                if context.candles[-1].open_time_ms == 0:
                    return StrategySignal(
                        strategy_name=self.name,
                        pair=context.pair,
                        interval=context.interval,
                        action=SignalAction.ENTER_LONG,
                        direction=SignalDirection.LONG,
                        confidence=Decimal("1"),
                        reason="Entry",
                        timestamp_ms=context.candles[-1].close_time_ms,
                        entry_price=Decimal("100"),
                        stop_loss=Decimal("90"),
                    )
                return StrategySignal.hold(
                    strategy_name=self.name,
                    pair=context.pair,
                    interval=context.interval,
                    timestamp_ms=context.candles[-1].close_time_ms,
                    reason="Hold",
                )

        engine = BacktestEngine(
            config=config,
            strategy_engine=StrategyEngine([LongEntryStrategy()]),
            risk_manager=RiskManager(),
        )
        
        # Candle 1: Entry 100. ATR 2.
        # Candle 2: Price 110. 
        # Fixed Trail: distance 5% = 5. Stop = 110 - 5 = 105.
        # ATR Trail: distance 0.5 * 2 = 1. Stop = 110 - 1 = 109.
        # If both run, stop might jump around or be suboptimal.
        # With mutual exclusivity, ATR should win because it updates later or suppresses the other.
        
        candles = [
            OHLCVCandle("BTC", "1h", 0, 3599999, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1")),
            OHLCVCandle("BTC", "1h", 3600000, 7199999, Decimal("100"), Decimal("110"), Decimal("109"), Decimal("110"), Decimal("1")),
        ]
        
        result = engine.run(candles)
        final_trade = result.trades[0]
        # Should have avoided the fixed trail value of 105.
        self.assertNotEqual(final_trade.metadata.get("atr_stop_loss"), Decimal("105"))
        self.assertEqual(final_trade.metadata.get("atr_stop_loss"), Decimal("109"))

if __name__ == "__main__":
    unittest.main()
