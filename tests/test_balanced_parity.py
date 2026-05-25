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
from app.strategies.base import StrategyEngine, SignalAction, SignalDirection
from app.strategies.hybrid_meta import HybridMetaStrategy
from app.risk.manager import RiskManager

def _candle(interval: str, open_time_ms: int, close: Decimal, volume: Decimal = Decimal("1000"), open_price: Decimal | None = None) -> OHLCVCandle:
    from app.data.candle_builder import interval_to_ms
    interval_ms = interval_to_ms(interval)
    op = open_price if open_price is not None else close
    return OHLCVCandle(
        pair="B-BTC_USDT",
        interval=interval,
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + interval_ms - 1,
        open=op,
        high=max(op, close) + Decimal("0.1"),
        low=min(op, close) - Decimal("0.1"),
        close=close,
        volume=volume,
        is_closed=True
    )

class BalancedParityTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.persistence.paper_state import PaperStateStore, PaperSessionStore
        self.db_path = "test_balanced_parity_state.db"
        self.session_path = "data/test_balanced_parity_session.json"
        self.csv_path = "test_balanced_parity_trades.csv"
        
        for p in [self.db_path, self.session_path, self.csv_path]:
            if Path(p).exists(): Path(p).unlink()

        self.state_store = PaperStateStore(self.db_path)
        self.session_store = PaperSessionStore(self.session_path)

        self.risk_settings = RiskSettings(
            balanced_breakout_enabled=True,
            balanced_breakout_volume_ratio_min=Decimal("1.5"),
            balanced_breakout_body_ratio_min=Decimal("0.1"),
            balanced_breakout_max_extension_atr=Decimal("100.0"),
            late_chase_block_enabled=False,
            false_breakout_filter_enabled=False,
            min_stop_distance_pct=Decimal("0"),
            min_stop_atr_multiple=Decimal("0"),
            min_entry_atr_pct=Decimal("0"),
            maker_fee_rate=Decimal("0"), taker_fee_rate=Decimal("0"), fee_gst_rate=Decimal("0"),
            slippage_pct=Decimal("0"), stop_slippage_pct=Decimal("0")
        )
        
        self.settings = Settings(
            paper_starting_equity=Decimal("10000"),
            risk=self.risk_settings,
            paper_intrabar_enabled=True,
            strategy_interval="1h",
            execution_interval="1m",
            paper_leverage=Decimal("3"),
            quote_to_margin_rate=Decimal("1")
        )

    def tearDown(self) -> None:
        self.state_store.close()
        for p in [self.db_path, self.session_path, self.csv_path]:
            if Path(p).exists(): Path(p).unlink()

    def test_balanced_breakout_parity(self) -> None:
        # Prepare 70 parent candles
        parent_candles = [_candle("1h", i * 3600000, Decimal("100")) for i in range(70)]
        
        # Child candles for parent index 65 (starts at 65 * 3600000 = 234,000,000)
        child_start = 234000000
        child_candles = []
        for i in range(10):
            # 0-1: flat 100
            # 2-4: breakout 102 (small enough to not be too extended)
            # 5-9: drop to 90
            price = Decimal("100") if i < 2 else Decimal("102") if i < 5 else Decimal("90")
            open_p = Decimal("100") if i < 2 else Decimal("101.5") if i < 5 else Decimal("91")
            vol = Decimal("1000") if i < 2 else Decimal("5000")
            child_candles.append(_candle("1m", child_start + i * 60000, price, volume=vol, open_price=open_p))
        
        # 1. Run Backtest
        config = BacktestConfig(
            pair="B-BTC_USDT", interval="1h", execution_interval="1m",
            strategy_name="hybrid_meta_v2", starting_equity=Decimal("10000"),
            leverage=Decimal("3"),
            paper_intrabar_enabled=True, strategy_interval="1h",
            balanced_breakout_enabled=True,
            balanced_breakout_volume_ratio_min=Decimal("1.5"),
            balanced_breakout_body_ratio_min=Decimal("0.1"),
            balanced_breakout_max_extension_atr=Decimal("100.0"),
            late_chase_block_enabled=False,
            false_breakout_filter_enabled=False,
            maker_fee_rate=Decimal("0"), taker_fee_rate=Decimal("0"), fee_gst_rate=Decimal("0"),
            slippage_pct=Decimal("0"), stop_slippage_pct=Decimal("0"),
            previous_parent_high=Decimal("101"),
            previous_parent_low=Decimal("99")
        )
        
        from app.strategies.hybrid_meta import HybridMetaV2Strategy
        strategy = HybridMetaV2Strategy()
        engine = BacktestEngine(config=config, strategy_engine=StrategyEngine([strategy]), risk_manager=RiskManager(self.risk_settings))
        bt_result = engine.run(parent_candles, execution_candles=child_candles)
        
        # 2. Run Paper Loop
        from app.live.summary_logger import PaperTradingSummaryLogger
        loop = PaperTradingLoop(self.settings, strategy_name="hybrid_meta_v2", state_store=self.state_store, session_store=self.session_store)
        loop.summary_logger = PaperTradingSummaryLogger(csv_path=str(self.csv_path))
        
        loop.series = {"B-BTC_USDT": CandleSeries(parent_candles)}
        loop.execution_series = {"B-BTC_USDT": CandleSeries()}
        loop._watchlist = ["B-BTC_USDT"]
        loop._current_interval = "1h"
        loop.gap_guards = {"B-BTC_USDT": MagicMock()}
        loop.gap_guards["B-BTC_USDT"].check.return_value.has_gap = False
        
        for c in child_candles:
            loop._on_candle(c)

        # 3. Compare
        self.assertGreater(len(bt_result.trades), 0, "Backtest should have at least one trade")
        self.assertEqual(len(bt_result.trades), len(loop.broker.fills) // 2, "Trade count mismatch")
        
        bt_trade = bt_result.trades[0]
        self.assertEqual(bt_trade.metadata.get("entry_type"), "balanced_breakout")
        
        paper_entry_price = loop.broker.fills[0].price
        self.assertEqual(bt_trade.entry_price, paper_entry_price, "Entry price mismatch")

if __name__ == "__main__":
    unittest.main()
