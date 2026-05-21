from __future__ import annotations

import unittest
import os
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from dataclasses import replace

from app.config import Settings, RiskSettings
from app.data.candle_builder import OHLCVCandle, CandleSeries
from app.live.paper_loop import PaperTradingLoop
from app.persistence.paper_state import PaperStateStore


def _candle(index: int, close: Decimal, pair: str = "B-BTC_USDT", interval: str = "5m") -> OHLCVCandle:
    # 5m = 300,000 ms
    open_time = index * 300000
    return OHLCVCandle(
        pair=pair,
        interval=interval,
        open_time_ms=open_time,
        close_time_ms=open_time + 299999,
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=Decimal("100"),
    )


class PaperLoopIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = Path(tempfile.mkdtemp())
        self.db_path = self.test_dir / "test_paper_state.db"
        self.csv_path = self.test_dir / "test_paper_trades.csv"
        
        self.settings = Settings(
            paper_starting_equity=Decimal("5000"),
            risk=RiskSettings(max_risk_per_trade_pct=Decimal("1")),
        )

    def tearDown(self) -> None:
        if hasattr(self, 'loop') and hasattr(self.loop, 'state_store'):
            self.loop.state_store.close()
        
        # Add a small delay for Windows file lock release
        import time
        time.sleep(0.1)
        try:
            shutil.rmtree(self.test_dir)
        except PermissionError:
            pass # Non-fatal for smoke test

    def test_loop_processes_candles_and_persists_state(self) -> None:
        # 1. Setup Loop
        self.loop = PaperTradingLoop(self.settings, strategy_name="adaptive_hybrid")
        loop = self.loop
        
        # Mock REST client to return empty but valid structure for gap re-fetch
        from unittest.mock import MagicMock
        loop.client.get_candles = MagicMock(return_value=[])
        
        # Override components to use test paths
        loop.state_store = PaperStateStore(str(self.db_path))
        # Re-init broker with the new state store
        loop.broker.state_store = loop.state_store
        
        from app.live.summary_logger import PaperTradingSummaryLogger
        loop.summary_logger = PaperTradingSummaryLogger(csv_path=str(self.csv_path), summary_every_n_candles=1)
        
        from app.data.gap_guard import CandleGapGuard
        loop.gap_guard = CandleGapGuard("5m")
        
        # Manually warm up with 10 candles
        loop.series = CandleSeries()
        for i in range(10):
            loop.series.add(_candle(i, Decimal("100") + i))
            
        # 2. Process more candles
        from app.risk.models import RiskDecision
        from app.strategies.base import SignalAction, SignalDirection, StrategySignal
        
        # Entry at candle 10 (no gap from 9)
        entry_candle = _candle(10, Decimal("110"))
        loop._on_candle(entry_candle)
        
        signal = StrategySignal(
            strategy_name="S", pair="B-BTC_USDT", interval="5m", 
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="test", timestamp_ms=entry_candle.close_time_ms,
            entry_price=Decimal("110"), stop_loss=Decimal("100"), take_profit=Decimal("150")
        )
        decision = RiskDecision(approved=True, reason="test", signal=signal, position_size=Decimal("1"))
        loop.broker.execute_decision(decision, market_price=Decimal("110"), timestamp_ms=entry_candle.close_time_ms)
        
        # Exit at candle 11
        exit_candle = _candle(11, Decimal("150"))
        loop._on_candle(exit_candle)
        
        # Candle 12 to 20
        for i in range(12, 21):
            loop._on_candle(_candle(i, Decimal("150")))
            
        # 3. Asserts
        snapshot = loop.broker.snapshot()
        
        # Equity is positive
        self.assertGreater(snapshot.equity, Decimal("0"))
        # We processed candle 10 to 20 = 11 candles
        self.assertEqual(loop.candle_count, 11)
        
        # CSV created
        self.assertTrue(self.csv_path.exists())
        
        # DB has a broker_state row
        saved_state = loop.state_store.load()
        self.assertIsNotNone(saved_state)
        self.assertAlmostEqual(saved_state["equity"], snapshot.equity)


if __name__ == "__main__":
    unittest.main()
