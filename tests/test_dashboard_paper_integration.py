from __future__ import annotations

import csv
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from app.config import Settings
from app.dashboard.server import DashboardRequestHandler, _normalize_dashboard_pair
from app.live.paper_loop import LivePaperState, get_live_state, _update_live_state

class MockServer:
    def __init__(self):
        self.settings = MagicMock()
        self.defaults = MagicMock()
        self._paper_lock = MagicMock()
        self._paper_loop = None
        self._paper_thread = None

class TestPaperDashboardIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self._old_cwd = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self.csv_path = Path("paper_trades.csv")
            
        self.server = MockServer()
        self.handler = MagicMock(spec=DashboardRequestHandler)
        self.handler.server = self.server
        self.handler._send_json = MagicMock()

    def tearDown(self) -> None:
        os.chdir(self._old_cwd)
        self._tmp.cleanup()

    def test_fees_paid_in_status(self) -> None:
        # Verify that fees_paid is included in the live state and sent to dashboard
        _update_live_state(fees_paid="15.75", running=True)
        
        self.handler.path = "/api/paper-status"
        DashboardRequestHandler.do_GET(self.handler)
        
        call_args = self.handler._send_json.call_args[0][0]
        self.assertEqual(call_args["fees_paid"], "15.75")

    def test_trades_endpoint_returns_data(self) -> None:
        # Verify /api/paper-trades returns trades from CSV
        header = ["timestamp", "pair", "direction", "net_pnl", "fees", "exit_reason"]
        trades = [
            {"timestamp": "2026-05-21T12:00:00Z", "pair": "B-BTC_USDT", "direction": "long", "net_pnl": "50", "fees": "1.2", "exit_reason": "take_profit"},
        ]
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=header)
            writer.writeheader()
            writer.writerows(trades)

        self.handler.path = "/api/paper-trades"
        # We need to use the real method since we mocked the handler
        DashboardRequestHandler._send_paper_trades(self.handler)
        
        call_args = self.handler._send_json.call_args[0][0]
        self.assertEqual(len(call_args["trades"]), 1)
        self.assertEqual(call_args["trades"][0]["fees"], "1.2")
        self.assertEqual(call_args["trades"][0]["exit_reason"], "take_profit")
        self.assertEqual(call_args["summary"]["closed_trades"], 1)
        self.assertEqual(call_args["summary"]["wins"], 1)
        self.assertEqual(call_args["summary"]["net_pnl"], "50")
        self.assertEqual(call_args["summary"]["fees"], "1.2")

    def test_trades_endpoint_summary_uses_total_fees_for_all_closed_trades(self) -> None:
        header = [
            "timestamp", "pair", "direction", "entry_price", "exit_price",
            "position_size", "gross_pnl", "fees", "total_fees", "net_pnl",
            "position_notional", "exit_reason",
        ]
        rows = [
            {
                "timestamp": "2026-05-21T12:00:00Z",
                "pair": "B-BTC_USDT",
                "direction": "long",
                "entry_price": "100",
                "exit_price": "110",
                "position_size": "1",
                "gross_pnl": "100",
                "fees": "1",
                "total_fees": "3",
                "net_pnl": "97",
                "position_notional": "1000",
                "exit_reason": "take_profit",
            },
            {
                "timestamp": "2026-05-21T13:00:00Z",
                "pair": "B-BTC_USDT",
                "direction": "long",
                "entry_price": "110",
                "exit_price": "105",
                "position_size": "1",
                "gross_pnl": "-50",
                "fees": "1",
                "total_fees": "2",
                "net_pnl": "-52",
                "position_notional": "800",
                "exit_reason": "stop_loss",
            },
        ]
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)

        DashboardRequestHandler._send_paper_trades(self.handler)

        payload = self.handler._send_json.call_args.args[0]
        summary = payload["summary"]
        self.assertEqual(len(payload["trades"]), 2)
        self.assertEqual(summary["closed_trades"], 2)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["gross_pnl"], "50")
        self.assertEqual(summary["net_pnl"], "45")
        self.assertEqual(summary["fees"], "5")
        self.assertEqual(summary["closed_notional"], "1800")
        self.assertEqual(summary["profit_factor"], str(Decimal("97") / Decimal("52")))

    def test_trades_endpoint_deduplicates_same_closed_trade(self) -> None:
        header = [
            "timestamp", "pair", "direction", "entry_price", "exit_price",
            "position_size", "net_pnl", "fees", "exit_reason", "hold_duration_candles",
        ]
        duplicate = {
            "timestamp": "2026-05-22T21:32:59Z",
            "pair": "B-EDEN_USDT",
            "direction": "short",
            "entry_price": "0.11648173",
            "exit_price": "0.12292871",
            "position_size": "14326.211685",
            "net_pnl": "-9153.19",
            "fees": "101.82",
            "exit_reason": "stop_loss",
            "hold_duration_candles": "317",
        }
        with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=header)
            writer.writeheader()
            writer.writerow(duplicate)
            writer.writerow({**duplicate, "hold_duration_candles": "0"})

        DashboardRequestHandler._send_paper_trades(self.handler)

        trades = self.handler._send_json.call_args.args[0]["trades"]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["hold_duration_candles"], "317")

    def test_safe_initial_state(self) -> None:
        # Verify that a fresh state has zero candles and null last_updated
        # Reset live state
        _update_live_state(
            running=False,
            candle_count=0,
            last_updated=None,
            equity="10000",
            starting_equity="10000"
        )
        
        state = get_live_state()
        self.assertEqual(state["candle_count"], 0)
        self.assertIsNone(state["last_updated"])
        self.assertEqual(state["equity"], "10000")

    def test_paper_status_reconciles_active_loop_when_running_flag_is_stale(self) -> None:
        loop = MagicMock()
        loop._watchlist = ["B-BSB_USDT", "B-EDEN_USDT"]
        loop._current_interval = "5m"

        with (
            patch(
                "app.live.paper_loop.get_live_state",
                return_value={"running": False, "pair": "", "interval": "", "error": ""},
            ),
            patch("app.live.paper_loop.get_active_loop", return_value=loop),
        ):
            self.handler.path = "/api/paper-status"
            DashboardRequestHandler.do_GET(self.handler)

        payload = self.handler._send_json.call_args.args[0]
        self.assertTrue(payload["running"])
        self.assertEqual(payload["pair"], "B-BSB_USDT, B-EDEN_USDT")
        self.assertEqual(payload["interval"], "5m")

    def test_fill_count_consistency(self) -> None:
        # Verify total_fills matches broker state
        _update_live_state(total_fills=42)
        state = get_live_state()
        self.assertEqual(state["total_fills"], 42)

    def test_pair_normalization_accepts_dashboard_display_pairs(self) -> None:
        self.assertEqual(_normalize_dashboard_pair("BSB-USDT"), "B-BSB_USDT")
        self.assertEqual(_normalize_dashboard_pair("B-BSB-USDT"), "B-BSB_USDT")
        self.assertEqual(_normalize_dashboard_pair("B-BSB_USDT"), "B-BSB_USDT")

    def test_paper_start_parses_false_strings_as_false(self) -> None:
        self.server.settings = Settings()
        self.server._paper_thread = None

        with (
            patch("app.live.paper_loop.get_live_state", return_value={"running": False}),
            patch("app.live.paper_loop.PaperTradingLoop") as loop_cls,
            patch("threading.Thread") as thread_cls,
        ):
            thread_cls.return_value = MagicMock()

            DashboardRequestHandler._handle_paper_start(
                self.handler,
                {
                    "pairs": ["B-BSB-USDT", "AXL-USDT"],
                    "interval": "15m",
                    "strategy": "adaptive_hybrid",
                    "allow_multi_pair_positions": "false",
                    "allow_same_pair_pyramiding": "false",
                    "paper_intrabar_enabled": "false",
                    "use_partial_parent_candle": "false",
                },
            )

        response = self.handler._send_json.call_args.args[0]
        self.assertEqual(response["pairs"], ["B-BSB_USDT", "B-AXL_USDT"])
        effective_settings = loop_cls.call_args.args[0]
        self.assertFalse(effective_settings.risk.allow_multi_pair_positions)
        self.assertFalse(effective_settings.risk.allow_same_pair_pyramiding)
        self.assertFalse(effective_settings.paper_intrabar_enabled)
        self.assertFalse(effective_settings.use_partial_parent_candle)

    def test_paper_start_applies_risk_and_execution_settings(self) -> None:
        self.server.settings = Settings()
        self.server._paper_thread = None

        with (
            patch("app.live.paper_loop.get_live_state", return_value={"running": False}),
            patch("app.live.paper_loop.PaperTradingLoop") as loop_cls,
            patch("threading.Thread") as thread_cls,
        ):
            thread_cls.return_value = MagicMock()

            DashboardRequestHandler._handle_paper_start(
                self.handler,
                {
                    "pair": "B-BTC_USDT",
                    "interval": "5m",
                    "strategy": "hybrid_meta_v2",
                    "leverage": "5",
                    "risk_pct": "3",
                    "max_daily_loss_pct": "7",
                    "paper_intrabar_enabled": "true",
                    "execution_interval": "1m",
                    "trailing_stop_enabled": "true",
                    "atr_dynamic_exits_enabled": "false",
                    "profit_lock_enabled": "true",
                },
            )

        effective_settings = loop_cls.call_args.args[0]
        self.assertEqual(effective_settings.paper_leverage, Decimal("5"))
        self.assertEqual(effective_settings.risk.max_risk_per_trade_pct, Decimal("3"))
        self.assertEqual(effective_settings.risk.max_daily_loss_pct, Decimal("7"))
        self.assertTrue(effective_settings.paper_intrabar_enabled)
        self.assertEqual(effective_settings.execution_interval, "1m")
        self.assertTrue(effective_settings.risk.trailing_stop_enabled)
        self.assertFalse(effective_settings.risk.atr_stop_enabled)
        self.assertFalse(effective_settings.risk.atr_take_profit_enabled)
        self.assertFalse(effective_settings.risk.atr_trailing_enabled)
        self.assertTrue(effective_settings.risk.profit_lock_enabled)

    def test_paper_start_rejects_empty_pair_list(self) -> None:
        self.server.settings = Settings()

        with patch("app.live.paper_loop.get_live_state", return_value={"running": False}):
            DashboardRequestHandler._handle_paper_start(
                self.handler,
                {"pairs": " , ", "interval": "5m", "strategy": "hybrid_meta_v2"},
            )

        response = self.handler._send_json.call_args.args[0]
        status = self.handler._send_json.call_args.kwargs["status"]
        self.assertIn("At least one", response["error"])
        self.assertEqual(status.value, 400)

    def test_paper_start_rejects_unsupported_execution_interval(self) -> None:
        self.server.settings = Settings()

        with patch("app.live.paper_loop.get_live_state", return_value={"running": False}):
            DashboardRequestHandler._handle_paper_start(
                self.handler,
                {
                    "pairs": "B-BSB_USDT",
                    "interval": "5m",
                    "strategy": "hybrid_meta_v2",
                    "paper_intrabar_enabled": "true",
                    "execution_interval": "2m",
                },
            )

        response = self.handler._send_json.call_args.args[0]
        status = self.handler._send_json.call_args.kwargs["status"]
        self.assertIn("Unsupported execution interval", response["error"])
        self.assertEqual(status.value, 400)

    def test_paper_reset_clears_live_state_and_server_loop(self) -> None:
        loop = MagicMock()
        self.server._paper_loop = loop
        Path("paper_trades.csv").write_text("timestamp,pair\nx,B-BTC_USDT\n", encoding="utf-8")
        _update_live_state(
            running=True,
            pair="B-BTC_USDT",
            watchlist=["B-BTC_USDT"],
            scanned_pairs={"B-BTC_USDT": "Closed"},
            interval="5m",
            strategy="hybrid_meta_v2",
            candle_count=12,
            equity="10050",
            starting_equity="10000",
            realized_pnl="50",
            fees_paid="1",
            open_positions=1,
            total_fills=2,
            positions_json="[{}]",
            equity_history_json="[{}]",
            candles_json='{"candles":[{}]}',
            error="old",
        )

        DashboardRequestHandler._handle_paper_reset(self.handler)

        loop.stop.assert_called_once()
        self.assertIsNone(self.server._paper_loop)
        self.assertIsNone(self.server._paper_thread)
        self.assertFalse(Path("paper_trades.csv").exists())
        state = get_live_state()
        self.assertFalse(state["running"])
        self.assertEqual(state["pair"], "")
        self.assertEqual(state["watchlist"], [])
        self.assertEqual(state["positions_json"], "[]")
        self.assertEqual(state["equity_history_json"], "[]")
        self.assertEqual(state["candles_json"], "{}")
        self.assertEqual(state["error"], "")

if __name__ == "__main__":
    unittest.main()
