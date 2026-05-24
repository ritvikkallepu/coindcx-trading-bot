from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock
from dataclasses import replace
from app.config import Settings, RiskSettings
from app.broker.models import PaperFill, PaperOrderSide, PaperPosition
from app.data.candle_builder import OHLCVCandle, CandleSeries
from app.live.paper_loop import PaperTradingLoop
from app.strategies.base import SignalAction, SignalDirection, StrategySignal, StrategyContext


def _candle(interval: str, open_time_ms: int, close: Decimal = Decimal("100"), pair: str = "B-BTC_USDT") -> OHLCVCandle:
    from app.data.candle_builder import interval_to_ms
    interval_ms = interval_to_ms(interval)
    return OHLCVCandle(
        pair=pair,
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
        from app.persistence.paper_state import PaperStateStore, PaperSessionStore
        from pathlib import Path
        self.db_path = "test_intrabar_state.db"
        self.session_path = "data/test_intrabar_session.json"
        
        # Clear previous
        if Path(self.db_path).exists(): Path(self.db_path).unlink()
        if Path(self.session_path).exists(): Path(self.session_path).unlink()

        self.state_store = PaperStateStore(self.db_path)
        self.session_store = PaperSessionStore(self.session_path)

        self.settings = Settings(
            paper_starting_equity=Decimal("10000"),
            paper_intrabar_enabled=True,
            strategy_interval="15m",
            execution_interval="1m",
            use_partial_parent_candle=False,
            max_entries_per_parent_candle=1,
            risk=RiskSettings(max_risk_per_trade_pct=Decimal("1"))
        )
        self.loop = PaperTradingLoop(
            self.settings, 
            strategy_name="adaptive_hybrid",
            state_store=self.state_store,
            session_store=self.session_store
        )
        self.loop.client.get_candles = MagicMock(return_value={"data": []})
        self.loop.gap_guards = {"B-BTC_USDT": MagicMock()}
        self.loop.gap_guards["B-BTC_USDT"].check.return_value.has_gap = False
        self.loop._current_interval = "15m"
        self.loop._watchlist = ["B-BTC_USDT"]

        # Mock strategy engine to return an entry signal on demand
        self.loop.strategy_engine.evaluate = MagicMock(return_value=[])

    def tearDown(self) -> None:
        from pathlib import Path
        self.state_store.close()
        if Path(self.db_path).exists(): Path(self.db_path).unlink()
        if Path(self.session_path).exists(): Path(self.session_path).unlink()

    def test_intrabar_entry_inside_parent_candle(self) -> None:
        pair = "B-BTC_USDT"
        self.loop.series = {pair: CandleSeries()}
        for i in range(10):
            self.loop.series[pair].add(_candle("15m", i * 900000, pair=pair))

        self.loop.execution_series = {pair: CandleSeries()}
        for i in range(150):
            self.loop.execution_series[pair].add(_candle("1m", i * 60000, pair=pair))

        self.loop._current_parent_open_ms = {pair: 9 * 900000}
        self.loop._entries_this_parent_candle = {pair: 0}
        
        parent_start = 10 * 900000 
        exec_candle = _candle("1m", parent_start + 120000, Decimal("105"), pair=pair)
        
        signal = StrategySignal(
            strategy_name="S", pair=pair, interval="15m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="test", timestamp_ms=parent_start - 1,
            entry_price=Decimal("105"), stop_loss=Decimal("100")
        )
        self.loop.strategy_engine.evaluate.return_value = [signal]

        self.loop._on_candle(exec_candle)

        self.assertEqual(len(self.loop.broker.positions), 1)
        self.assertEqual(self.loop._entries_this_parent_candle[pair], 1)
        position = self.loop.broker.open_positions()[0]
        self.assertEqual(position.metadata["entry_execution_candle_open_time_ms"], exec_candle.open_time_ms)
        self.assertEqual(position.metadata["entry_execution_candle_low"], exec_candle.low)

    def test_positions_payload_includes_trailing_audit_fields(self) -> None:
        pair = "B-BTC_USDT"
        self.loop.broker.positions[pair] = PaperPosition(
            pair=pair,
            direction=SignalDirection.LONG,
            quantity=Decimal("2"),
            entry_price=Decimal("100"),
            leverage=Decimal("5"),
            opened_at_ms=0,
            updated_at_ms=60_000,
            strategy_name="hybrid_meta_v2",
            stop_loss=Decimal("106"),
            take_profit=Decimal("120"),
            metadata={
                "atr_dynamic_exit_active": True,
                "atr_stop_loss": Decimal("106"),
                "take_profit_suppressed_by_trailing": True,
                "atr_best_price": Decimal("110"),
                "stop_type": "atr",
            },
        )

        payload = self.loop._positions_payload()[0]

        self.assertEqual(payload["active_trailing_stop"], "106")
        self.assertTrue(payload["take_profit_suppressed_by_trailing"])
        self.assertEqual(payload["atr_best_price"], "110")
        self.assertEqual(payload["stop_type"], "ATR")

    def test_intrabar_same_candle_live_update_replaces_and_reevaluates(self) -> None:
        pair = "B-BTC_USDT"
        self.loop.series = {pair: CandleSeries()}
        self.loop.series[pair].add(_candle("15m", 0, pair=pair))
        self.loop.execution_series = {pair: CandleSeries()}
        self.loop._current_parent_open_ms = {pair: 0}
        self.loop._entries_this_parent_candle = {pair: 0}

        first_update = _candle("1m", 60_000, Decimal("101"), pair=pair)
        stronger_update = _candle("1m", 60_000, Decimal("106"), pair=pair)

        self.loop._on_candle(first_update, allow_partial_execution=True)
        self.loop._on_candle(stronger_update, allow_partial_execution=True)

        self.assertEqual(len(self.loop.execution_series[pair]), 1)
        self.assertEqual(self.loop.execution_series[pair].latest().close, Decimal("106"))
        self.assertEqual(self.loop.candle_count, 1)
        self.assertEqual(self.loop.strategy_engine.evaluate.call_count, 2)

    def test_stream_snapshot_finalizes_previous_before_current_partial(self) -> None:
        calls: list[tuple[int, bool, bool]] = []

        def record(candle: OHLCVCandle, *, allow_partial_execution: bool = False) -> None:
            calls.append((candle.open_time_ms, allow_partial_execution, candle.is_closed))

        self.loop._on_candle = MagicMock(side_effect=record)

        first = replace(_candle("1m", 60_000), is_closed=False)
        second = replace(_candle("1m", 120_000), is_closed=False)

        self.loop._handle_stream_candle_snapshot(first)
        self.loop._handle_stream_candle_snapshot(second)

        self.assertEqual(
            calls,
            [
                (60_000, True, False),
                (60_000, False, True),
                (120_000, True, False),
            ],
        )

    def test_existing_position_exits_are_checked_before_dynamic_stop_ratchet(self) -> None:
        pair = "B-BTC_USDT"
        self.loop.series = {pair: CandleSeries()}
        self.loop.series[pair].add(_candle("15m", 0, pair=pair))
        self.loop.execution_series = {pair: CandleSeries()}
        self.loop._current_parent_open_ms = {pair: 0}
        self.loop._entries_this_parent_candle = {pair: 0}

        calls: list[str] = []
        self.loop.broker.process_candle = MagicMock(side_effect=lambda candle: calls.append("process") or [])
        self.loop.broker.update_dynamic_atr_exits = MagicMock(
            side_effect=lambda *args, **kwargs: calls.append("update_dynamic")
        )

        self.loop._on_candle(_candle("1m", 60_000, Decimal("105"), pair=pair))

        self.assertEqual(calls[:2], ["process", "update_dynamic"])

    def test_live_partial_update_filters_normal_entries_until_final_candle(self) -> None:
        pair = "B-BTC_USDT"
        self.loop.series = {pair: CandleSeries()}
        self.loop.series[pair].add(_candle("15m", 0, pair=pair))
        self.loop.execution_series = {pair: CandleSeries()}
        self.loop._current_parent_open_ms = {pair: 0}
        self.loop._entries_this_parent_candle = {pair: 0}

        candle = _candle("1m", 60_000, Decimal("105"), pair=pair)
        signal = StrategySignal(
            strategy_name="S", pair=pair, interval="15m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="normal strategy entry", timestamp_ms=119_999,
            entry_price=Decimal("105"), stop_loss=Decimal("100")
        )
        self.loop.strategy_engine.evaluate.return_value = [signal]

        self.loop._on_candle(candle, allow_partial_execution=True)

        self.assertEqual(len(self.loop.broker.positions), 0)

        self.loop._on_candle(candle)

        self.assertEqual(len(self.loop.broker.positions), 1)

    def test_enter_on_execution_close_blocks_breakout_entries_on_partial_updates(self) -> None:
        pair = "B-BTC_USDT"
        self.loop.series = {pair: CandleSeries()}
        self.loop.series[pair].add(_candle("15m", 0, pair=pair))
        self.loop.execution_series = {pair: CandleSeries()}
        self.loop._current_parent_open_ms = {pair: 0}
        self.loop._entries_this_parent_candle = {pair: 0}

        candle = replace(_candle("1m", 60_000, Decimal("105"), pair=pair), is_closed=False)
        signal = StrategySignal(
            strategy_name="S", pair=pair, interval="15m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="breakout", timestamp_ms=119_999,
            entry_price=Decimal("105"), stop_loss=Decimal("100"),
            metadata={"entry_type": "intrabar_reversal_breakout"},
        )
        self.loop.strategy_engine.evaluate.return_value = [signal]

        self.loop._on_candle(candle, allow_partial_execution=True)

        self.assertEqual(len(self.loop.broker.positions), 0)

    def test_paper_safety_blocks_pair_after_stop_loss(self) -> None:
        pair = "B-BTC_USDT"
        exit_candle = _candle("1m", 60_000, Decimal("95"), pair=pair)
        fill = PaperFill(
            fill_id="f1",
            order_id="o1",
            pair=pair,
            side=PaperOrderSide.SELL,
            quantity=Decimal("1"),
            price=Decimal("95"),
            fee=Decimal("1"),
            timestamp_ms=exit_candle.close_time_ms,
            realized_pnl=Decimal("-10"),
            metadata={"exit_trigger_type": "stop_loss"},
        )
        signal = StrategySignal(
            strategy_name="S", pair=pair, interval="15m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="test", timestamp_ms=exit_candle.close_time_ms,
            entry_price=Decimal("100"), stop_loss=Decimal("95")
        )

        self.loop._observe_closed_fill(fill, exit_candle)

        self.assertEqual(
            self.loop._paper_entry_safety_rejection(signal, exit_candle),
            "paper_pair_stop_loss_cooldown",
        )

    def test_paper_safety_blocks_same_direction_after_profitable_dynamic_stop(self) -> None:
        pair = "B-BTC_USDT"
        exit_candle = _candle("1m", 60_000, Decimal("110"), pair=pair)
        fill = PaperFill(
            fill_id="f1",
            order_id="o1",
            pair=pair,
            side=PaperOrderSide.SELL,
            quantity=Decimal("1"),
            price=Decimal("110"),
            fee=Decimal("1"),
            timestamp_ms=exit_candle.close_time_ms,
            realized_pnl=Decimal("100"),
            metadata={
                "exit_trigger_type": "stop_loss",
                "reason": "Dynamic ATR stop triggered.",
            },
        )
        long_signal = StrategySignal(
            strategy_name="S", pair=pair, interval="15m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="long again", timestamp_ms=exit_candle.close_time_ms,
            entry_price=Decimal("111"), stop_loss=Decimal("105")
        )
        short_signal = replace(
            long_signal,
            action=SignalAction.ENTER_SHORT,
            direction=SignalDirection.SHORT,
            reason="opposite direction",
            stop_loss=Decimal("115"),
        )

        self.loop._observe_closed_fill(fill, exit_candle)

        self.assertEqual(
            self.loop._paper_entry_safety_rejection(long_signal, exit_candle),
            "paper_reversal_same_direction_cooldown",
        )
        self.assertIsNone(
            self.loop._paper_entry_safety_rejection(short_signal, exit_candle)
        )

    def test_paper_safety_blocks_same_short_after_profitable_dynamic_stop(self) -> None:
        pair = "B-BTC_USDT"
        exit_candle = _candle("1m", 60_000, Decimal("90"), pair=pair)
        fill = PaperFill(
            fill_id="f1",
            order_id="o1",
            pair=pair,
            side=PaperOrderSide.BUY,
            quantity=Decimal("1"),
            price=Decimal("90"),
            fee=Decimal("1"),
            timestamp_ms=exit_candle.close_time_ms,
            realized_pnl=Decimal("100"),
            metadata={
                "exit_trigger_type": "stop_loss",
                "reason": "Dynamic ATR stop triggered.",
            },
        )
        short_signal = StrategySignal(
            strategy_name="S", pair=pair, interval="15m",
            action=SignalAction.ENTER_SHORT, direction=SignalDirection.SHORT,
            confidence=Decimal("1"), reason="short again", timestamp_ms=exit_candle.close_time_ms,
            entry_price=Decimal("89"), stop_loss=Decimal("95")
        )
        long_signal = replace(
            short_signal,
            action=SignalAction.ENTER_LONG,
            direction=SignalDirection.LONG,
            reason="opposite direction",
            stop_loss=Decimal("84"),
        )

        self.loop._observe_closed_fill(fill, exit_candle)

        self.assertEqual(
            self.loop._paper_entry_safety_rejection(short_signal, exit_candle),
            "paper_reversal_same_direction_cooldown",
        )
        self.assertIsNone(
            self.loop._paper_entry_safety_rejection(long_signal, exit_candle)
        )

    def test_paper_safety_allows_strong_reentry_during_cooldown(self) -> None:
        pair = "B-BTC_USDT"
        exit_candle = _candle("1m", 60_000, Decimal("95"), pair=pair)
        fill = PaperFill(
            fill_id="f1",
            order_id="o1",
            pair=pair,
            side=PaperOrderSide.SELL,
            quantity=Decimal("1"),
            price=Decimal("95"),
            fee=Decimal("1"),
            timestamp_ms=exit_candle.close_time_ms,
            realized_pnl=Decimal("-10"),
            metadata={"exit_trigger_type": "stop_loss"},
        )
        signal = StrategySignal(
            strategy_name="S", pair=pair, interval="15m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="strong continuation", timestamp_ms=exit_candle.close_time_ms,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
            metadata={
                "final_score": Decimal("0.72"),
                "agreement_ratio": Decimal("0.78"),
                "visual_score": Decimal("0.20"),
            },
        )

        self.loop._observe_closed_fill(fill, exit_candle)

        self.assertIsNone(self.loop._paper_entry_safety_rejection(signal, exit_candle))

    def test_paper_safety_throttles_risk_after_loss(self) -> None:
        pair = "B-BTC_USDT"
        self.loop._consecutive_losing_trades = 1
        signal = StrategySignal(
            strategy_name="S", pair=pair, interval="15m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="test", timestamp_ms=0,
            entry_price=Decimal("100"), stop_loss=Decimal("95"),
            metadata={"risk_multiplier": Decimal("1")},
        )

        throttled = self.loop._apply_paper_loss_throttle(signal)

        self.assertEqual(throttled.metadata["risk_multiplier"], Decimal("0.50"))
        self.assertTrue(throttled.metadata["paper_loss_throttle_active"])

    def test_intrabar_entry_limit_per_parent(self) -> None:
        pair = "B-BTC_USDT"
        self.loop.series = {pair: CandleSeries()}
        self.loop.series[pair].add(_candle("15m", 0, pair=pair))
        self.loop.execution_series = {pair: CandleSeries()}
        self.loop.execution_series[pair].add(_candle("1m", 0, pair=pair))
        self.loop._current_parent_open_ms = {pair: 0}
        self.loop._entries_this_parent_candle = {pair: 1} 

        exec_candle = _candle("1m", 60000, Decimal("105"), pair=pair)
        
        signal = StrategySignal(
            strategy_name="S", pair=pair, interval="15m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="test", timestamp_ms=59999,
            entry_price=Decimal("105"), stop_loss=Decimal("100")
        )
        self.loop.strategy_engine.evaluate.return_value = [signal]

        self.loop._on_candle(exec_candle)

        self.assertEqual(self.loop._entries_this_parent_candle[pair], 1)
        self.assertEqual(len(self.loop.broker.positions), 0)

    def test_parent_candle_change_resets_counter(self) -> None:
        pair = "B-BTC_USDT"
        self.loop.series = {pair: CandleSeries()}
        self.loop.series[pair].add(_candle("15m", 0, pair=pair))
        self.loop.execution_series = {pair: CandleSeries()}
        self.loop.execution_series[pair].add(_candle("1m", 0, pair=pair))
        self.loop._current_parent_open_ms = {pair: 0}
        self.loop._entries_this_parent_candle = {pair: 5} 

        new_parent_candle = _candle("15m", 900000, pair=pair)
        self.loop._on_candle(new_parent_candle)

        self.assertEqual(self.loop._entries_this_parent_candle[pair], 0)
        self.assertEqual(self.loop._current_parent_open_ms[pair], 900000)

    def test_partial_parent_candle_aggregation(self) -> None:
        pair = "B-BTC_USDT"
        self.loop.settings = replace(self.loop.settings, use_partial_parent_candle=True)
        self.loop.series = {pair: CandleSeries()}
        self.loop.series[pair].add(_candle("15m", 0, Decimal("100"), pair=pair)) 
        
        self.loop.execution_series = {pair: CandleSeries()}
        self.loop.execution_series[pair].add(_candle("1m", 900000, Decimal("101"), pair=pair))
        self.loop.execution_series[pair].add(_candle("1m", 960000, Decimal("102"), pair=pair))
        
        provisional_series = self.loop._build_provisional_htf_series(pair)
        
        self.assertEqual(len(provisional_series), 2)
        provisional = provisional_series.latest()
        self.assertEqual(provisional.open, Decimal("101"))
        self.assertEqual(provisional.close, Decimal("102"))
        self.assertEqual(provisional.high, Decimal("103"))
        self.assertEqual(provisional.interval, "15m")
        self.assertEqual(provisional.open_time_ms, 900000)


if __name__ == "__main__":
    unittest.main()
