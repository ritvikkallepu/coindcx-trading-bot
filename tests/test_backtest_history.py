from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from app.backtest.models import (
    BacktestConfig,
    BacktestEquityPoint,
    BacktestMetrics,
    BacktestResult,
)
from app.broker.models import PaperAccountSnapshot
from app.research.history import (
    _write_history_workbook,
    backtest_history_row,
    record_backtest_result,
)


def _result() -> BacktestResult:
    config = BacktestConfig(
        pair="B-SOL_USDT",
        interval="1h",
        starting_equity=Decimal("1000"),
        leverage=Decimal("3"),
        strategy_name="adaptive_hybrid",
        risk_per_trade_pct=Decimal("5"),
        stop_loss_pct=Decimal("2"),
        take_profit_pct=Decimal("4"),
        taker_fee_rate=Decimal("0.0005"),
        fee_gst_rate=Decimal("0.18"),
        slippage_pct=Decimal("0.02"),
        trailing_stop_enabled=True,
        trailing_stop_activation_pct=Decimal("1"),
        trailing_stop_distance_pct=Decimal("2"),
        atr_dynamic_exits_enabled=True,
        atr_period=14,
        atr_stop_multiple=Decimal("1.5"),
        atr_take_profit_multiple=Decimal("3"),
    )
    account = PaperAccountSnapshot(
        starting_equity=Decimal("1000"),
        realized_pnl=Decimal("50"),
        unrealized_pnl=Decimal("0"),
        fees_paid=Decimal("5"),
        equity=Decimal("1045"),
        open_position_count=0,
        open_notional=Decimal("0"),
    )
    metrics = BacktestMetrics(
        starting_equity=Decimal("1000"),
        final_equity=Decimal("1045"),
        total_return=Decimal("45"),
        total_return_pct=Decimal("4.5"),
        realized_pnl=Decimal("50"),
        unrealized_pnl=Decimal("0"),
        fees_paid=Decimal("5"),
        net_pnl=Decimal("45"),
        trade_count=3,
        winning_trades=2,
        losing_trades=1,
        win_rate_pct=Decimal("66.6667"),
        average_win=Decimal("30"),
        average_loss=Decimal("-15"),
        profit_factor=Decimal("2"),
        max_drawdown=Decimal("20"),
        max_drawdown_pct=Decimal("2"),
        sharpe_ratio=Decimal("1.2"),
    )
    return BacktestResult(
        config=config,
        candles_loaded=1000,
        candles_used=1000,
        metrics=metrics,
        final_account=account,
        equity_curve=[
            BacktestEquityPoint(
                timestamp_ms=1,
                close_price=Decimal("100"),
                equity=Decimal("1045"),
                realized_pnl=Decimal("45"),
                unrealized_pnl=Decimal("0"),
                fees_paid=Decimal("5"),
                open_position_count=0,
                open_notional=Decimal("0"),
            )
        ],
        trades=[],
        orders=[],
        fills=[],
    )


class BacktestHistoryTests(unittest.TestCase):
    def test_backtest_history_row_contains_key_metrics(self) -> None:
        row = backtest_history_row(_result(), source="cli")

        self.assertEqual(row["source"], "cli")
        self.assertEqual(row["pair"], "B-SOL_USDT")
        self.assertEqual(row["variant"], "adaptive_hybrid")
        self.assertIsNone(row["execution_interval"])
        self.assertEqual(row["intrabar_reentry_enabled"], False)
        self.assertEqual(row["max_reentries_per_candle"], 0)
        self.assertEqual(row["reentry_cooldown_candles"], 1)
        self.assertEqual(row["stop_loss_cooldown_candles"], 1)
        self.assertEqual(row["max_consecutive_losses"], 2)
        self.assertEqual(row["loss_cooldown_candles"], 4)
        self.assertEqual(row["risk_per_trade_pct"], Decimal("5"))
        self.assertEqual(row["stop_loss_pct"], Decimal("2"))
        self.assertEqual(row["take_profit_pct"], Decimal("4"))
        self.assertEqual(row["fee_gst_rate"], Decimal("0.18"))
        self.assertEqual(row["taker_fee_effective_rate"], Decimal("0.000590"))
        self.assertEqual(row["funding_fee_rate"], Decimal("0"))
        self.assertEqual(row["funding_interval_hours"], 8)
        self.assertEqual(row["funding_paid"], Decimal("0"))
        self.assertEqual(row["trailing_stop_enabled"], True)
        self.assertEqual(row["atr_dynamic_exits_enabled"], True)
        self.assertEqual(row["atr_stop_enabled"], True)
        self.assertEqual(row["atr_take_profit_enabled"], False)
        self.assertEqual(row["atr_trailing_enabled"], True)
        self.assertEqual(row["atr_entry_filter_enabled"], True)
        self.assertEqual(row["atr_policy_mode"], "router")
        self.assertEqual(row["atr_period"], 14)
        self.assertEqual(row["atr_stop_multiple"], Decimal("1.5"))
        self.assertEqual(row["atr_take_profit_multiple"], Decimal("3"))
        self.assertEqual(row["total_return_pct"], Decimal("4.5"))

    def test_record_backtest_result_writes_csv_jsonl_and_xlsx(self) -> None:
        with TemporaryDirectory() as tmp:
            history = record_backtest_result(
                _result(),
                source="dashboard",
                history_dir=Path(tmp),
            )

            self.assertTrue(history["saved"])
            self.assertEqual(history["rows"], 1)
            self.assertTrue(Path(str(history["csv"])).exists())
            self.assertTrue(Path(str(history["jsonl"])).exists())
            workbook_path = Path(str(history["workbook_xlsx"]))
            self.assertTrue(workbook_path.exists())
            with ZipFile(workbook_path) as workbook:
                sheet_xml = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
                self.assertIn("B-SOL_USDT", sheet_xml)
                self.assertIn("adaptive_hybrid", sheet_xml)

    def test_history_workbook_uses_stable_latest_copy_when_main_is_locked(self) -> None:
        with TemporaryDirectory() as tmp:
            workbook_path = Path(tmp) / "backtest_history.xlsx"
            with patch.object(
                Path,
                "replace",
                side_effect=[OSError("locked"), Path(tmp) / "backtest_history_latest.xlsx"],
            ):
                result = _write_history_workbook(
                    workbook_path=workbook_path,
                    rows=[backtest_history_row(_result(), source="dashboard")],
                    source_count=1,
                )

        self.assertEqual(
            result["fallback_workbook_xlsx"],
            str(Path(tmp) / "backtest_history_latest.xlsx"),
        )
        self.assertEqual(
            result["workbook_update_status"],
            "main_locked_latest_copy_updated",
        )


if __name__ == "__main__":
    unittest.main()
