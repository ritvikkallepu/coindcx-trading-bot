from __future__ import annotations

import unittest
import csv
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from dataclasses import replace

from app.config import Settings, RiskSettings
from app.data.candle_builder import OHLCVCandle, CandleSeries
from app.live.paper_loop import PaperTradingLoop
from app.backtest.engine import BacktestEngine
from app.backtest.models import BacktestConfig
from app.strategies.defaults import strategy_engine_for_name
from app.risk.manager import RiskManager


def _candle(interval: str, open_time_ms: int, close: Decimal, is_closed: bool = True) -> OHLCVCandle:
    from app.data.candle_builder import interval_to_ms
    interval_ms = interval_to_ms(interval)
    return OHLCVCandle(
        pair="B-BTC_USDT",
        interval=interval,
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + interval_ms - 1,
        open=close, # simple flat candle
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=Decimal("100"),
        is_closed=is_closed
    )


from app.strategies.base import Strategy, StrategySignal, SignalAction, SignalDirection, StrategyContext, StrategyEngine

class MockParityStrategy(Strategy):
    def __init__(self):
        self.name = "mock_parity"
        
    def evaluate(self, context: StrategyContext) -> StrategySignal:
        # Trigger on specific price
        # 110 is the close of parent 10 (index 10 in our 0-10 list).
        if context.latest_candle.close == Decimal("110"):
            return StrategySignal(
                strategy_name=self.name, pair=context.pair, interval=context.interval,
                action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
                confidence=Decimal("1"), reason="parity_test", timestamp_ms=context.latest_candle.close_time_ms,
                entry_price=Decimal("115"), stop_loss=Decimal("105"), take_profit=Decimal("130")
            )
        return StrategySignal.hold(
            strategy_name=self.name, pair=context.pair, interval=context.interval,
            timestamp_ms=context.latest_candle.close_time_ms, reason="no_signal"
        )

class ParityBacktestPaperTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.persistence.paper_state import PaperStateStore, PaperSessionStore
        from pathlib import Path
        self.db_path = "test_parity_state.db"
        self.session_path = "data/test_parity_session.json"
        self.csv_path = "test_parity_trades.csv"
        
        if Path(self.db_path).exists(): Path(self.db_path).unlink()
        if Path(self.session_path).exists(): Path(self.session_path).unlink()
        if Path(self.csv_path).exists(): Path(self.csv_path).unlink()

        self.state_store = PaperStateStore(self.db_path)
        self.session_store = PaperSessionStore(self.session_path)

        self.common_risk = RiskSettings(
            max_risk_per_trade_pct=Decimal("15"),
            max_total_risk_pct=Decimal("20"),
            maker_fee_rate=Decimal("0.0002"),
            taker_fee_rate=Decimal("0.0005"),
            fee_gst_rate=Decimal("0.18"),
            slippage_pct=Decimal("0"), # Task: 0 slippage for exact parity
            stop_slippage_pct=Decimal("0"),
            atr_stop_enabled=True,
            atr_take_profit_enabled=True,
            atr_trailing_enabled=True,
            atr_stop_multiple=Decimal("1.5"),
            atr_trailing_multiple=Decimal("2.0"),
            atr_take_profit_multiple=Decimal("2.4")
        )
        
        self.settings = Settings(
            paper_starting_equity=Decimal("10000"),
            risk=self.common_risk,
            paper_intrabar_enabled=True,
            strategy_interval="15m",
            execution_interval="1m"
        )
        
        self.strategy_name = "mock_parity"

    def tearDown(self) -> None:
        from pathlib import Path
        self.state_store.close()
        if Path(self.db_path).exists(): Path(self.db_path).unlink()
        if Path(self.session_path).exists(): Path(self.session_path).unlink()
        if Path(self.csv_path).exists(): Path(self.csv_path).unlink()

    def test_parity_same_candles_same_trades(self) -> None:
        # 1. Prepare shared input
        # 15m parent candles: 0, 900, 1800, ..., 10800
        parent_candles = [
            _candle("15m", i * 900000, Decimal("100") + i) for i in range(13)
        ]
        # Child candles for parent 11 (starts at 9,900,000)
        parent_11_start = 9900000 
        child_candles = []
        for i in range(15):
            price = Decimal("110") + i
            if i > 5: price = Decimal("115") + (i - 5) * 3 # rapidly reach 130
            child_candles.append(_candle("1m", parent_11_start + i * 60000, price))
        
        # 2. Run Backtest
        config = BacktestConfig(
            pair="B-BTC_USDT",
            interval="15m",
            execution_interval="1m",
            strategy_name=self.strategy_name,
            starting_equity=Decimal("10000"),
            risk_per_trade_pct=Decimal("2"), # Task: Lower risk
            leverage=Decimal("3"),
            paper_intrabar_enabled=True,
            strategy_interval="15m",
            atr_stop_multiple=Decimal("1.5"),
            atr_trailing_multiple=Decimal("2.0"),
            atr_take_profit_multiple=Decimal("2.4"),
            atr_dynamic_exits_enabled=False, 
            maker_fee_rate=Decimal("0"),
            taker_fee_rate=Decimal("0"),
            fee_gst_rate=Decimal("0"),
            slippage_pct=Decimal("0"),
            stop_slippage_pct=Decimal("0")
        )
        
        strategy = MockParityStrategy()
        strategy_engine = StrategyEngine([strategy])
        
        # Override evaluate to return a flat list
        def mock_engine_evaluate(context):
            sig = strategy.evaluate(context)
            return [sig]
        
        strategy_engine.evaluate = MagicMock(side_effect=mock_engine_evaluate)
        
        self.common_risk = replace(self.common_risk, max_risk_per_trade_pct=Decimal("2"))
        risk_manager = RiskManager(self.common_risk)
        
        engine = BacktestEngine(
            config=config,
            strategy_engine=strategy_engine,
            risk_manager=risk_manager
        )
        bt_result = engine.run(parent_candles, execution_candles=child_candles)
        
        # 3. Run Paper Loop (Mocked)
        from app.live.summary_logger import PaperTradingSummaryLogger
        
        # Override settings for parity
        parity_settings = replace(self.settings, risk=replace(self.settings.risk, 
            max_risk_per_trade_pct=Decimal("2"),
            maker_fee_rate=Decimal("0"), taker_fee_rate=Decimal("0"), 
            fee_gst_rate=Decimal("0"), slippage_pct=Decimal("0"), 
            stop_slippage_pct=Decimal("0")
        ))

        loop = PaperTradingLoop(
            parity_settings, 
            strategy_name="adaptive_hybrid",
            state_store=self.state_store,
            session_store=self.session_store
        )
        # Manually override engines for parity
        loop.strategy_engine = strategy_engine
        loop.risk_manager = risk_manager
        
        # Correctly override logger with test path
        loop.summary_logger = PaperTradingSummaryLogger(csv_path=str(self.csv_path))
        
        try:
            loop.client.get_candles = MagicMock(return_value={"data": []})
            loop.gap_guards = {"B-BTC_USDT": MagicMock()}
            loop.gap_guards["B-BTC_USDT"].check.return_value.has_gap = False
            loop._current_interval = "15m"
            loop._watchlist = ["B-BTC_USDT"]

            # Prepare series
            loop.series = {"B-BTC_USDT": CandleSeries()}
            loop.execution_series = {"B-BTC_USDT": CandleSeries()}
            # Process parent candles (warmup/standard flow)
            # Process up to parent 10 (index 10), which has close 110.
            for c in parent_candles[:11]:
                loop._on_candle(c)
                
            # Process child candles
            for c in child_candles:
                loop._on_candle(c)
                
            # 4. Compare
            # Ensure at least one trade executed
            self.assertGreater(len(bt_result.trades), 0)
            self.assertEqual(len(bt_result.trades), len(loop.broker.fills) // 2)
            
            # Read paper trades from CSV
            with open(self.csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                paper_trades = list(reader)
                
            self.assertEqual(len(bt_result.trades), len(paper_trades))
            
            if len(bt_result.trades) > 0:
                bt_trade = bt_result.trades[0]
                pp_trade = paper_trades[0]
                
                self.assertEqual(bt_trade.entry_price, Decimal(pp_trade["entry_price"]))
                self.assertEqual(bt_trade.exit_price, Decimal(pp_trade["exit_price"]))
                self.assertEqual(bt_trade.direction.value, pp_trade["direction"])
                self.assertAlmostEqual(bt_trade.net_pnl, Decimal(pp_trade["net_pnl"]), places=4)
        finally:
            pass

if __name__ == "__main__":
    unittest.main()
