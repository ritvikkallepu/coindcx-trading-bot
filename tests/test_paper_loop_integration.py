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
        self.session_path = self.test_dir / "test_paper_session.json"
        self.csv_path = self.test_dir / "test_paper_trades.csv"
        
        self.settings = Settings(
            paper_starting_equity=Decimal("5000"),
            quote_to_margin_rate=Decimal("1"),
            risk=RiskSettings(max_risk_per_trade_pct=Decimal("1")),
        )

    def tearDown(self) -> None:
        if hasattr(self, 'loop') and hasattr(self.loop, 'state_store'):
            self.loop.state_store.close()
        
        import time
        time.sleep(0.1)
        try:
            shutil.rmtree(self.test_dir)
        except PermissionError:
            pass

    def test_loop_processes_candles_and_persists_state(self) -> None:
        # 1. Setup Loop
        from app.persistence.paper_state import PaperStateStore, PaperSessionStore
        state_store = PaperStateStore(str(self.db_path))
        session_store = PaperSessionStore(str(self.session_path))

        self.loop = PaperTradingLoop(
            self.settings,
            strategy_name="adaptive_hybrid",
            state_store=state_store,
            session_store=session_store
        )
        loop = self.loop

        from unittest.mock import MagicMock
        loop.client.get_candles = MagicMock(return_value=[])

        from app.live.summary_logger import PaperTradingSummaryLogger
        loop.summary_logger = PaperTradingSummaryLogger(csv_path=str(self.csv_path), summary_every_n_candles=1)
        
        from app.data.gap_guard import CandleGapGuard
        loop.gap_guard = CandleGapGuard("5m")
        
        # Manually warm up with 10 candles
        pair = "B-BTC_USDT"
        loop._watchlist = [pair]
        loop.series = {pair: CandleSeries()}
        from app.data.gap_guard import CandleGapGuard
        loop.gap_guards = {pair: CandleGapGuard("5m")}
        for i in range(10):
            loop.series[pair].add(_candle(i, Decimal("100") + i))
            
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
        
        self.assertGreater(snapshot.equity, Decimal("0"))
        self.assertEqual(loop.candle_count, 11)
        self.assertTrue(self.csv_path.exists())

        saved_state = loop.state_store.load()
        self.assertIsNotNone(saved_state)
        self.assertAlmostEqual(saved_state["equity"], snapshot.equity)

        # Session state should also be saved
        self.assertTrue(self.session_path.exists())
        saved_session = session_store.load_session()
        self.assertEqual(saved_session["candle_count"], 11)

    def test_paper_loop_sizes_risk_from_initial_equity_not_accumulated_profit(self) -> None:
        from app.persistence.paper_state import PaperSessionStore
        from app.data.gap_guard import CandleGapGuard
        from app.strategies.base import SignalAction, SignalDirection, StrategySignal
        from unittest.mock import MagicMock

        settings = Settings(
            paper_starting_equity=Decimal("1000"),
            paper_leverage=Decimal("5"),
            quote_to_margin_rate=Decimal("1"),
            risk=RiskSettings(
                max_risk_per_trade_pct=Decimal("10"),
                max_leverage=5,
                slippage_pct=Decimal("0"),
                stop_slippage_pct=Decimal("0"),
                maker_fee_rate=Decimal("0"),
                taker_fee_rate=Decimal("0"),
            ),
        )
        self.loop = PaperTradingLoop(
            settings,
            strategy_name="adaptive_hybrid",
            state_store=PaperStateStore(str(self.db_path)),
            session_store=PaperSessionStore(str(self.session_path)),
        )
        pair = "B-BTC_USDT"
        self.loop._watchlist = [pair]
        self.loop._current_interval = "5m"
        self.loop.series = {pair: CandleSeries()}
        self.loop.gap_guards = {pair: CandleGapGuard("5m")}
        for i in range(10):
            self.loop.series[pair].add(_candle(i, Decimal("100"), pair=pair))

        self.loop.broker.realized_pnl = Decimal("1000")
        signal = StrategySignal(
            strategy_name="S",
            pair=pair,
            interval="5m",
            action=SignalAction.ENTER_LONG,
            direction=SignalDirection.LONG,
            confidence=Decimal("1"),
            reason="entry",
            timestamp_ms=_candle(10, Decimal("100"), pair=pair).close_time_ms,
            entry_price=Decimal("100"),
            stop_loss=Decimal("90"),
            take_profit=Decimal("120"),
        )
        self.loop.strategy_engine.evaluate = MagicMock(return_value=[signal])

        self.loop._on_candle(_candle(10, Decimal("100"), pair=pair))

        position = self.loop.broker.positions[pair]
        self.assertEqual(position.quantity, Decimal("10"))
        self.assertEqual(position.metadata["risk_base_mode"], "initial_equity")
        self.assertEqual(position.metadata["risk_base_amount"], Decimal("1000"))
        self.assertEqual(position.metadata["risk_percent_used"], Decimal("10.0"))

    def test_paper_loop_reconnects_when_websocket_returns_unexpectedly(self) -> None:
        from app.persistence.paper_state import PaperSessionStore
        from unittest.mock import MagicMock, patch

        state_store = PaperStateStore(str(self.db_path))
        session_store = PaperSessionStore(str(self.session_path))
        self.loop = PaperTradingLoop(
            self.settings,
            strategy_name="adaptive_hybrid",
            state_store=state_store,
            session_store=session_store,
        )
        self.loop.client.get_candles = MagicMock(return_value=[])
        self.loop._websocket_reconnect_delay_seconds = MagicMock(return_value=0)
        self.loop._sleep_before_reconnect = MagicMock()

        run_calls: list[int] = []

        class FakeWebSocketClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def run(self, subscriptions) -> None:
                run_calls.append(len(subscriptions))
                if len(run_calls) >= 2:
                    raise KeyboardInterrupt()

            def stop(self) -> None:
                pass

        with patch("app.live.paper_loop.CoinDCXFuturesWebSocketClient", FakeWebSocketClient):
            self.loop.run("B-BTC_USDT", "5m")

        self.assertEqual(len(run_calls), 2)
        self.assertTrue(self.session_path.exists())


if __name__ == "__main__":
    unittest.main()
