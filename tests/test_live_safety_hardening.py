import unittest
import json
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch
from pathlib import Path
from app.live.live_loop import LiveTradingLoop
from app.config import Settings, RiskSettings
from app.strategies.base import StrategySignal, SignalAction, SignalDirection
from app.execution.live import LiveExecutionReport, LivePositionSnapshot, LiveSyncResult
from app.risk.models import OpenPosition, RiskContext, RiskDecision

class TestLiveSafetyHardening(unittest.TestCase):
    def setUp(self):
        from dataclasses import replace
        self.settings = Settings()
        self.settings = replace(
            self.settings,
            live_pilot_dry_run=True,
            live_trading_enabled=False,
            risk=replace(
                self.settings.risk, 
                live_kill_switch=False,
                live_risk_approval_enabled=True
            )
        )
        self.state_path = Path("data/live_state.json")
        self.state_dir = Path("data/live_state")
        if self.state_path.exists():
            self.state_path.unlink()
        if self.state_dir.exists():
            for path in self.state_dir.glob("*.json"):
                path.unlink()

    def tearDown(self):
        if self.state_path.exists():
            self.state_path.unlink()
        if self.state_dir.exists():
            for path in self.state_dir.glob("*.json"):
                path.unlink()

    def test_kill_switch_persistence_restart(self):
        # 1. Manually set kill switch in state file
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump({"kill_switch_active": True}, f)
        
        # 2. Instantiate loop and check if it loads it
        loop = LiveTradingLoop(self.settings)
        self.assertTrue(loop.kill_switch_active)
        
        # 3. Verify evaluate is skipped
        mock_candle = MagicMock()
        mock_candle.pair = "B-BTC_USDT"
        with self.assertLogs('app.live.live_loop', level='INFO') as cm:
            loop._process_trading_candle(mock_candle)
            self.assertTrue(any("KILL SWITCH ACTIVE" in line for line in cm.output))

    @patch("app.execution.live.LiveExecutionEngine.process_decision")
    def test_tpsl_safety_failure_triggers_kill_switch(self, mock_process):
        loop = LiveTradingLoop(self.settings)
        loop.kill_switch_active = False
        
        # Mock a safety failure report
        report = LiveExecutionReport(
            accepted=False,
            dry_run=False,
            reason="SAFETY FAILURE: TPSL sync failed",
            safety_failure=True,
            submitted_to_exchange=True,
            requires_manual_reconciliation=True
        )
        mock_process.return_value = report
        
        signal = StrategySignal(
            pair="B-BTC_USDT", 
            action=SignalAction.ENTER_LONG, 
            direction=SignalDirection.LONG,
            confidence=1.0,
            strategy_name="test_strat",
            interval="15m",
            reason="test",
            timestamp_ms=1000
        )
        candle = MagicMock()
        candle.close = Decimal("50000")
        
        with patch.object(loop.risk_manager, 'evaluate') as mock_risk:
            mock_risk.return_value = MagicMock(approved=True, position_size=Decimal("1"))
            loop._handle_signal(signal, candle)
        
        self.assertTrue(loop.kill_switch_active)
        self.assertTrue(loop.local_state["kill_switch_active"])
        
        # Check if persisted to this loop's isolated run-state file.
        with open(loop.state_path, "r") as f:
            state = json.load(f)
            self.assertTrue(state["kill_switch_active"])

    def test_live_runs_use_isolated_state_files(self):
        first = LiveTradingLoop(
            self.settings,
            pairs=["B-STO_USDT"],
            strategy_name="fib_ma_pullback",
            interval="1h",
            execution_interval="5m",
        )
        second = LiveTradingLoop(
            self.settings,
            pairs=["B-NFP_USDT"],
            strategy_name="hybrid_meta_v2",
            interval="5m",
            execution_interval="1m",
        )

        self.assertNotEqual(first.state_path, second.state_path)
        self.assertEqual(first.state_path.parent, Path("data/live_state"))
        self.assertEqual(second.state_path.parent, Path("data/live_state"))
        self.assertEqual(first.local_state["run_id"], first.run_id)
        self.assertEqual(second.local_state["run_id"], second.run_id)

    def test_missing_stop_loss_blocks_new_entries(self):
        from dataclasses import replace
        live_settings = replace(self.settings, live_trading_enabled=True)
        loop = LiveTradingLoop(live_settings)
        
        # Add a position without stop loss
        loop.local_state["positions"]["B-BTC_USDT"] = {
            "pair": "B-BTC_USDT",
            "active_pos": "1",
            "avg_price": "50000",
            "source": "exchange",
            "stop_loss_trigger": None # Missing stop
        }
        
        signal = StrategySignal(
            pair="B-ETH_USDT", 
            action=SignalAction.ENTER_LONG, 
            direction=SignalDirection.LONG,
            entry_price=Decimal("2500"),
            stop_loss=Decimal("2400"),
            confidence=1.0,
            strategy_name="test_strat",
            interval="15m",
            reason="test",
            timestamp_ms=1000
        )
        
        # Check RiskManager evaluation
        tradable_equity = Decimal("100000")
        risk_context = RiskContext(
            signal=signal,
            account_equity=tradable_equity,
            available_equity=tradable_equity, 
            open_positions=loop._get_open_positions(),
            trading_mode="live",
            live_trading_enabled=True,
            unit_contract_value=Decimal("1")
        )
        
        decision = loop.risk_manager.evaluate(risk_context)
        self.assertFalse(decision.approved)
        self.assertIn("UNSAFE LIVE STATE", decision.reason)

    def test_missing_stop_loss_alerts_once_until_condition_is_resolved(self):
        from dataclasses import replace

        live_settings = replace(self.settings, live_trading_enabled=True)
        loop = LiveTradingLoop(live_settings)
        pair = "B-BTC_USDT"
        position = {
            "pair": pair,
            "active_pos": "1",
            "avg_price": "50000",
            "source": "exchange",
            "stop_loss_trigger": None,
        }
        loop.local_state["positions"][pair] = position

        with patch.object(loop.alert, "alert") as mock_alert:
            loop._get_open_positions()
            loop._get_open_positions()
            mock_alert.assert_called_once_with(
                f"UNSAFE LIVE STATE: {pair} has NO STOP-LOSS!",
                level="CRITICAL",
            )

            position["stop_loss_trigger"] = "49000"
            loop._get_open_positions()
            position["stop_loss_trigger"] = None
            loop._get_open_positions()

            self.assertEqual(mock_alert.call_count, 2)

    def test_capital_per_pair_sets_live_signal_sizing_equity(self):
        from dataclasses import replace

        live_settings = replace(
            self.settings,
            risk=replace(
                self.settings.risk,
                max_margin_per_pair=Decimal("1000"),
                live_min_confidence=Decimal("0.60"),
            ),
        )
        loop = LiveTradingLoop(
            live_settings,
            pairs=["B-BSB_USDT", "B-XAN_USDT"],
            starting_equity=Decimal("5000"),
            leverage=Decimal("3"),
        )
        loop.local_state["tradable_base"] = "5000"

        signal = StrategySignal(
            pair="B-BSB_USDT",
            action=SignalAction.ENTER_LONG,
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            confidence=Decimal("1"),
            strategy_name="test_strat",
            interval="5m",
            reason="test",
            timestamp_ms=1000,
        )
        candle = MagicMock()
        candle.pair = "B-BSB_USDT"
        candle.close = Decimal("100")
        candle.close_time_ms = 1000
        captured_contexts: list[RiskContext] = []

        def capture_context(context: RiskContext) -> RiskDecision:
            captured_contexts.append(context)
            return RiskDecision(False, "test rejection", context.signal)

        loop.risk_manager.evaluate = MagicMock(side_effect=capture_context)

        loop._handle_signal(signal, candle)

        self.assertEqual(len(captured_contexts), 1)
        context = captured_contexts[0]
        self.assertEqual(context.account_equity, Decimal("5000"))
        self.assertEqual(context.available_equity, Decimal("5000"))
        self.assertEqual(context.sizing_equity, Decimal("1000"))

    def test_pre_entry_exchange_sync_blocks_duplicate_pair_position(self):
        from dataclasses import replace

        pair = "B-BSB_USDT"
        live_settings = replace(
            self.settings,
            trading_mode="live",
            live_trading_enabled=True,
            live_pilot_dry_run=False,
            risk=replace(
                self.settings.risk,
                live_min_confidence=Decimal("0.60"),
            ),
        )
        loop = LiveTradingLoop(
            live_settings,
            pairs=[pair],
            starting_equity=Decimal("1000"),
            leverage=Decimal("3"),
        )
        loop.local_state["positions"] = {}
        loop.local_state["position_metadata"] = {}
        position = LivePositionSnapshot(
            position_id="pos-bsb",
            pair=pair,
            active_pos=Decimal("117"),
            quantity=Decimal("117"),
            direction=SignalDirection.LONG,
            avg_price=Decimal("0.24953"),
            liquidation_price=None,
            locked_margin=Decimal("1000"),
            locked_order_margin=Decimal("0"),
            take_profit_trigger=None,
            stop_loss_trigger=Decimal("0.2375"),
            leverage=Decimal("3"),
            margin_type="isolated",
            margin_currency_short_name="INR",
            settlement_currency_avg_price=Decimal("98"),
            updated_at=1780811100000,
            raw={"id": "pos-bsb", "pair": pair},
        )
        signal = StrategySignal(
            pair=pair,
            action=SignalAction.ENTER_LONG,
            direction=SignalDirection.LONG,
            entry_price=Decimal("0.25"),
            stop_loss=Decimal("0.2375"),
            confidence=Decimal("1"),
            strategy_name="test_strat",
            interval="5m",
            reason="test",
            timestamp_ms=1000,
        )
        candle = MagicMock()
        candle.pair = pair
        candle.close = Decimal("0.25")
        candle.close_time_ms = 1780811100000

        with patch.object(loop.execution_engine.sync, "fetch_position", return_value=position):
            with patch.object(loop.risk_manager, "evaluate") as mock_evaluate:
                loop._handle_signal(signal, candle)

        mock_evaluate.assert_not_called()
        self.assertIn(pair, loop.local_state["positions"])
        self.assertEqual(
            loop.local_state["recent_diagnostics"][-1]["reason"],
            "exchange_position_already_open",
        )
        self.assertIn(signal.pair, loop.local_state["position_metadata"])

    def test_reconcile_includes_portfolio_positions_not_only_watchlist_or_local_state(self):
        loop = LiveTradingLoop(self.settings, pairs=["B-BSB_USDT"])
        seen_pairs: list[str] = []

        def refresh_portfolio() -> None:
            loop._portfolio_positions = {
                "B-OLD_USDT": {
                    "pair": "B-OLD_USDT",
                    "active_pos": "1",
                    "avg_price": "1",
                    "source": "exchange",
                }
            }

        with patch.object(loop, "_refresh_portfolio_snapshot", side_effect=refresh_portfolio):
            with patch.object(loop, "_reconcile_pair", side_effect=lambda pair: seen_pairs.append(pair)):
                with patch.object(loop, "_reconcile_funding_transactions"):
                    with patch.object(loop, "_reconcile_daily_loss"):
                        with patch.object(loop, "_update_dashboard_state"):
                            with patch.object(loop, "save_local_state"):
                                loop.reconcile()

        self.assertIn("B-BSB_USDT", seen_pairs)
        self.assertIn("B-OLD_USDT", seen_pairs)

    def test_reconcile_recovers_position_metadata_from_prior_run_state(self):
        from dataclasses import replace

        pair = "B-BSB_USDT"
        live_settings = replace(
            self.settings,
            trading_mode="live",
            live_trading_enabled=True,
            live_pilot_dry_run=False,
        )
        old_state = {
            "run_id": "old-run",
            "running": True,
            "last_updated": "2026-06-07T06:10:00+00:00",
            "position_metadata": {
                pair: {
                    "initial_entry_price": "0.24953",
                    "initial_stop_loss": "0.2375",
                    "initial_r": "0.01203",
                    "peak_price": "0.25967",
                    "max_r_hit": "0.84",
                    "strategy_name": "hybrid_meta_v2",
                }
            },
        }
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "old-run.json").write_text(json.dumps(old_state))
        loop = LiveTradingLoop(
            live_settings,
            pairs=[pair, "B-XAN_USDT"],
            starting_equity=Decimal("1000"),
            leverage=Decimal("3"),
        )
        loop.local_state["positions"] = {}
        loop.local_state["position_metadata"] = {}
        position = LivePositionSnapshot(
            position_id="pos-bsb",
            pair=pair,
            active_pos=Decimal("117"),
            quantity=Decimal("117"),
            direction=SignalDirection.LONG,
            avg_price=Decimal("0.24953"),
            liquidation_price=None,
            locked_margin=Decimal("1000"),
            locked_order_margin=Decimal("0"),
            take_profit_trigger=None,
            stop_loss_trigger=Decimal("0.2375"),
            leverage=Decimal("3"),
            margin_type="isolated",
            margin_currency_short_name="INR",
            settlement_currency_avg_price=Decimal("98"),
            updated_at=1780811100000,
            raw={"id": "pos-bsb", "pair": pair},
        )

        with patch.object(loop.execution_engine.sync, "fetch_position", return_value=position):
            loop._reconcile_pair(pair)

        recovered = loop.local_state["position_metadata"][pair]
        self.assertEqual(recovered["max_r_hit"], "0.84")
        self.assertEqual(recovered["strategy_name"], "hybrid_meta_v2")

    def test_cooldown_after_reconciliation_stop(self):
        from dataclasses import replace
        from app.data.candle_builder import interval_to_ms
        live_settings = replace(self.settings, trading_mode="live", live_trading_enabled=True)
        loop = LiveTradingLoop(live_settings)
        pair = "B-BTC_USDT"
        
        # 1. Local state has active exchange position
        loop.local_state["positions"][pair] = {
            "pair": pair,
            "active_pos": "1",
            "avg_price": "50000",
            "source": "exchange"
        }
        
        # 2. Mock exchange position as GONE (stopped out)
        with patch.object(loop.execution_engine.sync, 'fetch_position', return_value=None):
            with patch.object(loop, '_process_closed_position'):
                loop._reconcile_pair(pair)
        
        # 3. Check if cooldown is set
        self.assertIn(pair, loop._stopped_out_candles)
        last_stop_ms = loop._stopped_out_candles[pair]
        
        # 4. Verify same-candle re-entry is blocked
        # Align to interval boundary
        interval_ms = interval_to_ms(loop.interval)
        aligned_ts = (last_stop_ms // interval_ms) * interval_ms
        
        mock_candle = MagicMock()
        mock_candle.pair = pair
        mock_candle.interval = loop.interval
        mock_candle.open_time_ms = aligned_ts
        mock_candle.close_time_ms = aligned_ts + interval_ms
        mock_candle.is_closed = True
        
        with patch.object(loop, '_check_dry_run_stops', return_value=False):
            ready_time_ms = (
                aligned_ts
                + interval_ms
                + loop.settings.live_closed_candle_buffer_ms
                + 1
            )
            with patch('time.time', return_value=ready_time_ms / 1000.0):
                with self.assertLogs('app.live.live_loop', level='DEBUG') as cm:
                    loop._on_candle(mock_candle)
                    found = any("cooldown blocked entry" in line.lower() for line in cm.output)
                    if not found:
                         print(f"DEBUG: cm.output={cm.output}")
                    self.assertTrue(found)

if __name__ == "__main__":
    unittest.main()
