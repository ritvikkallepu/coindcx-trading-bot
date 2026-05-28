from __future__ import annotations

import unittest
from decimal import Decimal

from app.backtest.engine import BacktestEngine
from app.backtest.models import BacktestConfig
from app.data.candle_builder import OHLCVCandle
from app.risk.manager import RiskManager
from app.config import RiskSettings
from app.strategies.base import SignalAction, SignalDirection, Strategy, StrategyEngine, StrategySignal


class MockBankruptStrategy(Strategy):
    name = "bankrupt"
    def __init__(self, entry_price: Decimal, stop_loss: Decimal):
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.triggered = False

    def evaluate(self, context):
        if not self.triggered:
            self.triggered = True
            return StrategySignal(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                action=SignalAction.ENTER_LONG,
                direction=SignalDirection.LONG,
                confidence=Decimal("1"),
                reason="Force bankruptcy",
                timestamp_ms=context.candles[-1].close_time_ms,
                entry_price=self.entry_price,
                stop_loss=self.stop_loss,
            )
        return StrategySignal.hold(
            strategy_name=self.name,
            pair=context.pair,
            interval=context.interval,
            timestamp_ms=context.candles[-1].close_time_ms,
            reason="Already triggered",
        )


class TwoEntryStrategy(Strategy):
    name = "two_entry"
    def evaluate(self, context):
        if not context.features.get("open_position"):
            return StrategySignal(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                action=SignalAction.ENTER_LONG,
                direction=SignalDirection.LONG,
                confidence=Decimal("1"),
                reason="Entry",
                timestamp_ms=context.candles[-1].close_time_ms,
                entry_price=context.candles[-1].close,
                stop_loss=context.candles[-1].close * Decimal("0.1"), # 90% risk
            )
        return StrategySignal.hold(
            strategy_name=self.name,
            pair=context.pair,
            interval=context.interval,
            timestamp_ms=context.candles[-1].close_time_ms,
            reason="Position open",
        )


class EquityDepletionTests(unittest.TestCase):
    def test_backtest_blocks_entries_when_equity_depleted(self) -> None:
        config = BacktestConfig(
            pair="B-SOL_USDT",
            interval="1h",
            starting_equity=Decimal("1000"),
            leverage=Decimal("1"),
            risk_per_trade_pct=Decimal("100"),
            compound_risk_equity=False,
            atr_period=1,
            atr_entry_filter_enabled=False,
            profit_locking_enabled=False
        )
        risk_settings = RiskSettings(max_open_positions=1, max_risk_per_trade_pct=Decimal("100"), max_daily_loss_pct=Decimal("100"), max_total_risk_pct=Decimal("100"))
        engine = BacktestEngine(config=config, strategy_engine=StrategyEngine([TwoEntryStrategy()]), risk_manager=RiskManager(settings=risk_settings))
        
        candles = [
            OHLCVCandle("B-SOL_USDT", "1h", 0, 3599999, Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("1")),
            OHLCVCandle("B-SOL_USDT", "1h", 3600000, 7199999, Decimal("100"), Decimal("100"), Decimal("10"), Decimal("10"), Decimal("1")),
            OHLCVCandle("B-SOL_USDT", "1h", 7200000, 10799999, Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("1")),
            OHLCVCandle("B-SOL_USDT", "1h", 10800000, 14399999, Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("1")),
        ]
        
        result = engine.run(candles)
        rejections = [r for r in result.reports if not r.accepted and r.signal.action != SignalAction.HOLD]
        self.assertEqual(len(result.trades), 1)
        self.assertTrue(len(rejections) > 0)
        self.assertTrue(any("margin" in r.reason.lower() or "planned risk" in r.reason.lower() or "cooldown" in r.reason.lower() for r in rejections))

    def test_entry_rejected_when_required_margin_exceeds_available_equity(self) -> None:
        config = BacktestConfig(
            pair="B-SOL_USDT",
            interval="1h",
            starting_equity=Decimal("100"),
            leverage=Decimal("20"), 
            risk_per_trade_pct=Decimal("50"),
            atr_period=1,
            atr_entry_filter_enabled=False,
            profit_locking_enabled=False
        )
        risk_settings = RiskSettings(max_open_positions=1, max_risk_per_trade_pct=Decimal("100"), max_total_risk_pct=Decimal("100"), max_leverage=100)
        engine = BacktestEngine(config=config, strategy_engine=StrategyEngine([MockBankruptStrategy(Decimal("1"), Decimal("0.99"))]), risk_manager=RiskManager(settings=risk_settings))
        
        candles = [OHLCVCandle("B-SOL_USDT", "1h", 0, 3599999, Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"))]
        result = engine.run(candles)
        rejections = [r for r in result.reports if not r.accepted]
        # print(f"DEBUG: {[r.reason for r in rejections]}")
        self.assertEqual(len(result.trades), 0)
        self.assertTrue(any("margin" in r.reason.lower() or "risk decision rejected" in r.reason.lower() for r in rejections))

    def test_entry_rejected_when_planned_risk_exceeds_available_equity(self) -> None:
        class HighRiskStrategy(Strategy):
            name = "high_risk"
            def evaluate(self, context):
                if context.candles[-1].open_time_ms == 0:
                    return StrategySignal(
                        strategy_name=self.name,
                        pair=context.pair,
                        interval=context.interval,
                        action=SignalAction.ENTER_LONG,
                        direction=SignalDirection.LONG,
                        confidence=Decimal("1"),
                        reason="Entry 1",
                        timestamp_ms=context.candles[-1].close_time_ms,
                        entry_price=Decimal("100"),
                        stop_loss=Decimal("50"),
                        metadata={"allow_scale_in": True},
                    )
                if context.candles[-1].open_time_ms == 3600000:
                    return StrategySignal(
                        strategy_name=self.name,
                        pair=context.pair,
                        interval=context.interval,
                        action=SignalAction.ENTER_LONG,
                        direction=SignalDirection.LONG,
                        confidence=Decimal("1"),
                        reason="Entry 2",
                        timestamp_ms=context.candles[-1].close_time_ms,
                        entry_price=Decimal("100"),
                        stop_loss=Decimal("50"),
                        metadata={"allow_scale_in": True},
                    )
                return StrategySignal.hold(
                    strategy_name=self.name,
                    pair=context.pair,
                    interval=context.interval,
                    timestamp_ms=context.candles[-1].close_time_ms,
                    reason="Hold",
                )

        config = BacktestConfig(
            pair="B-SOL_USDT",
            interval="1h",
            starting_equity=Decimal("1000"),
            leverage=Decimal("1"),
            risk_per_trade_pct=Decimal("80"),
            compound_risk_equity=False,
            atr_period=1,
            atr_entry_filter_enabled=False,
            stop_loss_cooldown_candles=0,
            profit_locking_enabled=False
        )
        risk_settings = RiskSettings(max_open_positions=1, max_risk_per_trade_pct=Decimal("100"), max_daily_loss_pct=Decimal("100"), max_leverage=100, max_total_risk_pct=Decimal("100"))
        engine = BacktestEngine(config=config, strategy_engine=StrategyEngine([HighRiskStrategy()]), risk_manager=RiskManager(settings=risk_settings))
        
        candles = [
            OHLCVCandle("B-SOL_USDT", "1h", 0, 3599999, Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("1")),
            OHLCVCandle("B-SOL_USDT", "1h", 3600000, 7199999, Decimal("100"), Decimal("100"), Decimal("50"), Decimal("50"), Decimal("1")),
            OHLCVCandle("B-SOL_USDT", "1h", 7200000, 10799999, Decimal("50"), Decimal("50"), Decimal("50"), Decimal("50"), Decimal("1")),
        ]
        
        result = engine.run(candles)
        self.assertEqual(len(result.trades), 1)
        rejections = [r for r in result.reports if not r.accepted and r.signal.action == SignalAction.ENTER_LONG]
        self.assertTrue(any(("planned risk" in r.reason.lower() or "margin" in r.reason.lower()) and "exceeds available equity" in r.reason.lower() for r in rejections))

    def test_equity_before_after_trade_is_recorded_correctly(self) -> None:
        config = BacktestConfig(
            pair="B-SOL_USDT",
            interval="1h",
            starting_equity=Decimal("1000"),
            leverage=Decimal("1"),
            risk_per_trade_pct=Decimal("10"), 
            compound_risk_equity=False,
            atr_period=1,
            atr_entry_filter_enabled=False,
            profit_locking_enabled=False
        )
        risk_settings = RiskSettings(max_open_positions=1, max_risk_per_trade_pct=Decimal("100"), max_total_risk_pct=Decimal("100"))
        engine = BacktestEngine(config=config, strategy_engine=StrategyEngine([TwoEntryStrategy()]), risk_manager=RiskManager(settings=risk_settings))
        
        candles = [
            OHLCVCandle("B-SOL_USDT", "1h", 0, 3599999, Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("1")),
            OHLCVCandle("B-SOL_USDT", "1h", 3600000, 7199999, Decimal("100"), Decimal("100"), Decimal("90"), Decimal("90"), Decimal("1")),
        ]
        
        result = engine.run(candles)
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.metadata.get("equity_before_trade"), Decimal("1000"))
        self.assertEqual(trade.metadata.get("equity_after_trade"), Decimal("900"))

    def test_r_multiple_uses_planned_risk(self) -> None:
        config = BacktestConfig(
            pair="B-SOL_USDT",
            interval="1h",
            starting_equity=Decimal("1000"),
            leverage=Decimal("1"),
            risk_per_trade_pct=Decimal("10"),
            compound_risk_equity=False,
            atr_period=1,
            atr_entry_filter_enabled=False,
            profit_locking_enabled=False
        )
        risk_settings = RiskSettings(max_open_positions=1, max_risk_per_trade_pct=Decimal("100"), max_total_risk_pct=Decimal("100"))
        
        class ProfitStrategy(Strategy):
            name = "profit"
            def evaluate(self, context):
                if not context.features.get("open_position"):
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
                        take_profit=Decimal("110"),
                    )
                return StrategySignal.hold(
                    strategy_name=self.name,
                    pair=context.pair,
                    interval=context.interval,
                    timestamp_ms=context.candles[-1].close_time_ms,
                    reason="Hold",
                )

        engine = BacktestEngine(config=config, strategy_engine=StrategyEngine([ProfitStrategy()]), risk_manager=RiskManager(settings=risk_settings))
        
        candles = [
            OHLCVCandle("B-SOL_USDT", "1h", 0, 3599999, Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("1")),
            OHLCVCandle("B-SOL_USDT", "1h", 3600000, 7199999, Decimal("100"), Decimal("110"), Decimal("100"), Decimal("110"), Decimal("1")),
        ]
        
        result = engine.run(candles)
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertAlmostEqual(trade.metadata.get("r_multiple"), Decimal("1.0"), places=2)

if __name__ == "__main__":
    unittest.main()
