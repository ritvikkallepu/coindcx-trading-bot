from __future__ import annotations

import unittest
import os
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from dataclasses import replace
from unittest.mock import MagicMock

from app.config import Settings, RiskSettings
from app.data.candle_builder import OHLCVCandle, CandleSeries
from app.live.paper_loop import PaperTradingLoop
from app.persistence.paper_state import PaperSessionStore, PaperStateStore


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
        self.loop.broker.locked_profit = Decimal("1000")
        self.loop.broker.tradable_base = Decimal("1000")
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
        self.assertEqual(position.metadata["risk_base_mode"], "tradable_equity")
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

    def test_add_pair_to_watchlist_subscribes_running_websocket(self) -> None:
        state_store = PaperStateStore(str(self.db_path))
        session_store = PaperSessionStore(str(self.session_path))
        self.loop = PaperTradingLoop(
            self.settings,
            strategy_name="adaptive_hybrid",
            state_store=state_store,
            session_store=session_store,
        )
        self.loop._current_interval = "5m"
        self.loop._watchlist = ["B-BTC_USDT"]
        self.loop._replace_subscription_snapshot(["B-BTC_USDT"], "5m")
        self.loop._warm_up = MagicMock()
        self.loop._warm_up_execution = MagicMock()

        ws_client = MagicMock()
        self.loop._ws_client = ws_client

        result = self.loop.add_pair_to_watchlist("B-AXL_USDT", background=False)

        self.assertTrue(result["added"])
        self.assertEqual(self.loop._watchlist_snapshot(), ["B-BTC_USDT", "B-AXL_USDT"])
        ws_client.subscribe.assert_called_once()
        channels = [subscription.channel_name for subscription in ws_client.subscribe.call_args.args[0]]
        self.assertIn("B-AXL_USDT_5m-futures", channels)
        self.assertIn("B-AXL_USDT@orderbook@50-futures", channels)

    def test_update_watchlist_removes_pair_from_future_subscription_snapshot(self) -> None:
        state_store = PaperStateStore(str(self.db_path))
        session_store = PaperSessionStore(str(self.session_path))
        self.loop = PaperTradingLoop(
            self.settings,
            strategy_name="adaptive_hybrid",
            state_store=state_store,
            session_store=session_store,
        )
        self.loop._current_interval = "5m"
        self.loop._watchlist = ["B-BTC_USDT", "B-AXL_USDT"]
        self.loop.series = {"B-BTC_USDT": CandleSeries(), "B-AXL_USDT": CandleSeries()}
        self.loop._replace_subscription_snapshot(["B-BTC_USDT", "B-AXL_USDT"], "5m")

        result = self.loop.update_watchlist(["B-BTC_USDT"], background=False)

        self.assertTrue(result["updated"])
        self.assertEqual(result["removed"], ["B-AXL_USDT"])
        self.assertEqual(self.loop._watchlist_snapshot(), ["B-BTC_USDT"])
        channels = [subscription.channel_name for subscription in self.loop._subscriptions_snapshot()]
        self.assertFalse(any("B-AXL_USDT" in channel for channel in channels))

    def test_removed_pair_with_open_position_stays_subscribed_for_exit_management(self) -> None:
        state_store = PaperStateStore(str(self.db_path))
        session_store = PaperSessionStore(str(self.session_path))
        self.loop = PaperTradingLoop(
            self.settings,
            strategy_name="adaptive_hybrid",
            state_store=state_store,
            session_store=session_store,
        )
        self.loop._current_interval = "5m"
        self.loop._watchlist = ["B-BTC_USDT", "B-AXL_USDT"]
        self.loop.series = {"B-BTC_USDT": CandleSeries(), "B-AXL_USDT": CandleSeries()}
        self.loop._replace_subscription_snapshot(["B-BTC_USDT", "B-AXL_USDT"], "5m")
        self.loop._publish_live_snapshot = MagicMock()
        self.loop._save_session = MagicMock()
        open_position = MagicMock()
        open_position.pair = "B-AXL_USDT"
        self.loop.broker.open_positions = MagicMock(return_value=[open_position])

        result = self.loop.update_watchlist(["B-BTC_USDT"], background=False)

        self.assertTrue(result["updated"])
        self.assertEqual(result["removed"], ["B-AXL_USDT"])
        channels = [subscription.channel_name for subscription in self.loop._subscriptions_snapshot()]
        self.assertTrue(any("B-AXL_USDT" in channel for channel in channels))

    def test_strategy_features_apply_major_liquid_profile_by_pair_kind(self) -> None:
        state_store = PaperStateStore(str(self.db_path))
        session_store = PaperSessionStore(str(self.session_path))
        self.loop = PaperTradingLoop(
            self.settings,
            strategy_name="hybrid_meta_v2",
            state_store=state_store,
            session_store=session_store,
        )

        sol_features = self.loop._strategy_features("B-SOL_USDT")
        bsb_features = self.loop._strategy_features("B-BSB_USDT")

        self.assertEqual(sol_features["pair_profile"]["key"], "major_liquid")
        self.assertEqual(
            sol_features["backtest_config"]["balanced_breakout_volume_ratio_min"],
            Decimal("1.25"),
        )
        self.assertEqual(
            sol_features["backtest_config"]["profile_risk_multiplier"],
            Decimal("0.75"),
        )
        self.assertEqual(bsb_features["pair_profile"]["key"], "volatile_alt")
        self.assertEqual(
            bsb_features["backtest_config"]["balanced_breakout_volume_ratio_min"],
            self.settings.risk.balanced_breakout_volume_ratio_min,
        )

    def test_pair_overrides_change_risk_leverage_and_strategy_per_pair(self) -> None:
        state_store = PaperStateStore(str(self.db_path))
        session_store = PaperSessionStore(str(self.session_path))
        self.loop = PaperTradingLoop(
            self.settings,
            strategy_name="hybrid_meta_v2",
            state_store=state_store,
            session_store=session_store,
            pair_overrides={
                "B-BTC_USDT": {
                    "strategy": "bb_dynamic_grid",
                    "leverage": "2",
                    "risk_pct": "3",
                    "max_margin_usage_pct": "40",
                    "trailing_stop_enabled": False,
                    "atr_dynamic_exits_enabled": False,
                    "max_entries_per_parent_candle": 2,
                }
            },
        )

        risk = self.loop._risk_settings_for_pair("B-BTC_USDT")
        features = self.loop._strategy_features("B-BTC_USDT")

        self.assertEqual(self.loop._strategy_name_for_pair("B-BTC_USDT"), "bb_dynamic_grid")
        self.assertEqual(self.loop._paper_leverage_for_pair("B-BTC_USDT"), Decimal("2"))
        self.assertEqual(self.loop._max_entries_per_parent_candle_for_pair("B-BTC_USDT"), 2)
        self.assertEqual(risk.max_risk_per_trade_pct, Decimal("3"))
        self.assertEqual(risk.max_margin_usage_pct, Decimal("40"))
        self.assertFalse(risk.trailing_stop_enabled)
        self.assertFalse(risk.atr_stop_enabled)
        self.assertFalse(features["backtest_config"]["trailing_stop_enabled"])
        self.assertFalse(features["backtest_config"]["atr_dynamic_exits_enabled"])


if __name__ == "__main__":
    unittest.main()
