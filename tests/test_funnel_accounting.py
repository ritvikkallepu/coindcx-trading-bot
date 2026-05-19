from __future__ import annotations

import unittest
from decimal import Decimal
from dataclasses import replace

from app.backtest.engine import BacktestEngine
from app.backtest.models import BacktestConfig, backtest_signal_funnel
from app.data.candle_builder import OHLCVCandle
from app.risk.manager import RiskManager
from app.broker.models import PaperExecutionReport, PaperAccountSnapshot, PaperExecutionStatus, PaperOrder, PaperOrderSide, PaperOrderStatus
from app.strategies.base import SignalAction, SignalDirection, SignalFunnelReason, Strategy, StrategyEngine, StrategySignal


class FunnelAccountingTests(unittest.TestCase):
    def test_raw_candidates_are_fully_explained(self) -> None:
        # 1 raw long, 1 raw short
        # 1 executed, 1 blocked by threshold
        config = BacktestConfig(pair="BTC", interval="1h", starting_equity=Decimal("1000"), leverage=Decimal("1"))
        
        signal_exec = StrategySignal(
            strategy_name="test", pair="BTC", interval="1h", 
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG, 
            confidence=Decimal("1"), reason="Exec", timestamp_ms=1,
            metadata={"signal_funnel_raw_candidate": True, "signal_funnel_raw_direction": "long"}
        )
        
        signal_blocked = StrategySignal(
            strategy_name="test", pair="BTC", interval="1h", 
            action=SignalAction.HOLD, direction=SignalDirection.SHORT, 
            confidence=Decimal("0"), reason="Weak", timestamp_ms=2,
            metadata={
                "signal_funnel_raw_candidate": True, 
                "signal_funnel_raw_direction": "short",
                "funnel_reason": SignalFunnelReason.BELOW_ENTRY_THRESHOLD
            }
        )
        
        account = PaperAccountSnapshot(Decimal("1000"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1000"), 0, Decimal("0"))
        
        order = PaperOrder(
            order_id="1", pair="BTC", side=PaperOrderSide.BUY, action=SignalAction.ENTER_LONG, 
            quantity=Decimal("1"), price=Decimal("100"), leverage=Decimal("1"), 
            status=PaperOrderStatus.FILLED, created_at_ms=1
        )
        
        reports = [
            PaperExecutionReport(accepted=True, status=PaperExecutionStatus.FILLED, reason="Filled", account=account, signal=signal_exec, order=order),
            PaperExecutionReport(accepted=False, status=PaperExecutionStatus.REJECTED, reason="Blocked", account=account, signal=signal_blocked)
        ]
        
        funnel = backtest_signal_funnel(config=config, reports=reports, trades=[], candles_loaded=10, candles_used=10)
        
        self.assertEqual(funnel["raw_candidates_total"], 2)
        self.assertEqual(funnel["executed_entries"], 1)
        self.assertEqual(funnel["explained_rejections_total"], 1)
        self.assertEqual(funnel["unexplained_candidates_total"], 0)
        self.assertEqual(funnel["accounted_candidates_total"], 2)
        self.assertEqual(funnel["block_reasons"][SignalFunnelReason.BELOW_ENTRY_THRESHOLD.value], 1)

    def test_unexplained_candidates_are_tracked(self) -> None:
        config = BacktestConfig(pair="BTC", interval="1h", starting_equity=Decimal("1000"), leverage=Decimal("1"))
        
        # Signal with raw candidate flag but NO mapped reason and NO execution
        signal_unmapped = StrategySignal(
            strategy_name="test", pair="BTC", interval="1h", 
            action=SignalAction.HOLD, direction=SignalDirection.LONG, 
            confidence=Decimal("0"), reason="Mysterious", timestamp_ms=1,
            metadata={"signal_funnel_raw_candidate": True, "signal_funnel_raw_direction": "long"}
        )
        
        account = PaperAccountSnapshot(Decimal("1000"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1000"), 0, Decimal("0"))
        
        reports = [
            PaperExecutionReport(accepted=False, status=PaperExecutionStatus.REJECTED, reason="Mysterious", account=account, signal=signal_unmapped)
        ]
        
        funnel = backtest_signal_funnel(config=config, reports=reports, trades=[], candles_loaded=10, candles_used=10)
        
        self.assertEqual(funnel["raw_candidates_total"], 1)
        self.assertEqual(funnel["unexplained_candidates_total"], 0)
        self.assertEqual(funnel["block_reasons"][SignalFunnelReason.STRATEGY_HOLD_UNMAPPED.value], 1)

    def test_b_setup_failure_categorization(self) -> None:
        # Simulate HybridMeta logic
        config = BacktestConfig(pair="BTC", interval="1h", starting_equity=Decimal("1000"), leverage=Decimal("1"))
        
        signal = StrategySignal(
            strategy_name="hybrid_meta", pair="BTC", interval="1h", 
            action=SignalAction.HOLD, direction=SignalDirection.LONG, 
            confidence=Decimal("0"), reason="Quality blocked", timestamp_ms=1,
            metadata={
                "signal_funnel_raw_candidate": True, 
                "signal_funnel_raw_direction": "long",
                "funnel_reason": SignalFunnelReason.BELOW_B_SETUP_THRESHOLD.value
            }
        )
        
        account = PaperAccountSnapshot(Decimal("1000"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1000"), 0, Decimal("0"))
        reports = [PaperExecutionReport(accepted=False, status=PaperExecutionStatus.REJECTED, reason="Quality", account=account, signal=signal)]
        
        funnel = backtest_signal_funnel(config=config, reports=reports, trades=[], candles_loaded=10, candles_used=10)
        self.assertEqual(funnel["block_reasons"][SignalFunnelReason.BELOW_B_SETUP_THRESHOLD.value], 1)

    def test_risk_rejection_mapping(self) -> None:
        config = BacktestConfig(pair="BTC", interval="1h", starting_equity=Decimal("1000"), leverage=Decimal("1"))
        
        # Risk manager rejection typically doesn't have funnel_reason yet, it relies on text matching in the fallback
        signal = StrategySignal(
            strategy_name="test", pair="BTC", interval="1h", 
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG, 
            confidence=Decimal("1"), reason="Entry", timestamp_ms=1,
            metadata={"signal_funnel_raw_candidate": True, "signal_funnel_raw_direction": "long"}
        )
        
        account = PaperAccountSnapshot(Decimal("1000"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1000"), 0, Decimal("0"))
        reports = [
            PaperExecutionReport(accepted=False, status=PaperExecutionStatus.REJECTED, reason="Risk decision rejected: Required margin exceeds available equity", account=account, signal=signal)
        ]
        
        funnel = backtest_signal_funnel(config=config, reports=reports, trades=[], candles_loaded=10, candles_used=10)
        self.assertEqual(funnel["block_reasons"][SignalFunnelReason.EXPOSURE_MARGIN_BLOCKED.value], 1)
        self.assertEqual(funnel["explained_rejections_total"], 1)

if __name__ == "__main__":
    unittest.main()
