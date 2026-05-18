from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from app.research.sweep import (
    DEFAULT_INTERVALS,
    DEFAULT_SWEEP_PAIRS,
    SUMMARY_FIELDS,
    StrategyVariant,
    SweepConfig,
    _error_row,
    _market_tasks,
    _parameter_configs,
    _scheduled_runs,
    default_strategy_variants,
    parse_bool_grid,
    parse_csv_list,
    parse_decimal_list,
    parse_optional_decimal_list,
    parse_trailing_profiles,
    parse_until,
    strategy_variants_for_names,
)
from app.strategies.hybrid_meta import HybridMetaStrategy
from app.research.excel import write_sweep_workbook


class ResearchSweepTests(unittest.TestCase):
    def test_parse_until_uses_same_day_for_future_local_time(self) -> None:
        now = datetime(2026, 5, 17, 4, 35, tzinfo=timezone.utc)

        target = parse_until("08:30", now=now)

        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.date(), now.date())
        self.assertEqual(target.hour, 8)
        self.assertEqual(target.minute, 30)

    def test_parse_until_rolls_to_next_day_for_past_time(self) -> None:
        now = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)

        target = parse_until("08:30", now=now)

        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.day, 18)

    def test_parse_csv_list_uses_default_when_empty(self) -> None:
        self.assertEqual(parse_csv_list("", ("a", "b")), ("a", "b"))
        self.assertEqual(parse_csv_list("x, y", DEFAULT_SWEEP_PAIRS), ("x", "y"))

    def test_default_strategy_variants_have_unique_names(self) -> None:
        names = [variant.name for variant in default_strategy_variants()]

        self.assertEqual(len(names), len(set(names)))
        self.assertIn("adaptive_default", names)
        self.assertIn("hybrid_meta", names)
        self.assertIn("hybrid_meta_v2", names)
        self.assertIn("bb_strict_volume", names)
        self.assertIn("grid_bb_dynamic", names)

    def test_strategy_variant_aliases_select_only_requested_variants(self) -> None:
        variants = strategy_variants_for_names(("hybrid_meta", "adaptive_hybrid"))

        self.assertEqual([variant.name for variant in variants], ["hybrid_meta", "adaptive_default"])

    def test_grid_parsers_handle_sweep_values(self) -> None:
        self.assertEqual(parse_decimal_list("1, 2.5", ()), (Decimal("1"), Decimal("2.5")))
        self.assertEqual(
            parse_optional_decimal_list("default,4", ()),
            (None, Decimal("4")),
        )
        self.assertEqual(parse_bool_grid("off,on", ()), (False, True))
        profiles = parse_trailing_profiles("off,2:3")
        self.assertEqual(len(profiles), 2)
        self.assertFalse(profiles[0].enabled)
        self.assertTrue(profiles[1].enabled)
        self.assertEqual(profiles[1].activation_pct, Decimal("2"))
        self.assertEqual(profiles[1].distance_pct, Decimal("3"))

    def test_time_limited_schedule_varies_risk_and_leverage_early(self) -> None:
        config = SweepConfig(
            pairs=("B-ZEC_USDT", "B-RAVE_USDT"),
            intervals=("5m", "1h"),
            risk_per_trade_pct_values=tuple(Decimal(value) for value in ("1", "2", "3")),
            leverage_values=tuple(Decimal(value) for value in ("1", "2", "3")),
            take_profit_pct_values=(None, Decimal("5")),
            trailing_profiles=parse_trailing_profiles("off,1:2"),
            atr_dynamic_exits_values=(False, True),
        )
        variant = StrategyVariant(
            name="hybrid_meta",
            family="hybrid_meta",
            factory=lambda: HybridMetaStrategy(),
        )

        scheduled = list(
            _scheduled_runs(
                parameter_configs=tuple(_parameter_configs(config)),
                market_tasks=tuple(
                    _market_tasks(
                        variants=(variant,),
                        pairs=config.pairs,
                        intervals=config.intervals,
                    )
                ),
            )
        )[:12]

        risks = {run_config.risk_per_trade_pct for run_config, *_ in scheduled}
        leverages = {run_config.leverage for run_config, *_ in scheduled}
        self.assertGreater(len(risks), 1)
        self.assertGreater(len(leverages), 1)

    def test_default_intervals_focus_on_active_intraday_research(self) -> None:
        self.assertEqual(DEFAULT_INTERVALS, ("5m", "15m", "1h"))

    def test_error_rows_match_summary_fields_after_execution_guard_columns(self) -> None:
        row = _error_row(
            pair="B-SOL_USDT",
            interval="1h",
            variant="hybrid_meta",
            family="hybrid_meta",
            error=ValueError("no candles"),
        )

        self.assertEqual(set(row), set(SUMMARY_FIELDS))
        self.assertIn("execution_interval", row)
        self.assertIn("stop_loss_cooldown_candles", row)
        self.assertIn("max_consecutive_losses", row)

    def test_write_sweep_workbook_creates_xlsx_with_results_sheet(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "results.xlsx"
            write_sweep_workbook(
                output,
                rows=[
                    {
                        "pair": "B-SOL_USDT",
                        "interval": "1h",
                        "variant": "adaptive_default",
                        "total_return_pct": "13.45",
                        "profit_factor": "2.6",
                        "trade_count": 18,
                        "error": "",
                    }
                ],
                assumptions={"status": "finished", "intervals": "5m, 15m, 1h"},
            )

            self.assertTrue(output.exists())
            with ZipFile(output) as workbook:
                names = set(workbook.namelist())
                self.assertIn("xl/workbook.xml", names)
                self.assertIn("xl/worksheets/sheet2.xml", names)
                sheet_xml = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
                self.assertIn("B-SOL_USDT", sheet_xml)
                self.assertIn("adaptive_default", sheet_xml)


if __name__ == "__main__":
    unittest.main()
