from __future__ import annotations

import csv
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.config import Settings
from app.research.quick_validator import (
    QuickValidationConfig,
    _select_top_rows,
    run_quick_validator,
)
from app.research.sweep import SUMMARY_FIELDS, SweepConfig, strategy_variants_for_names


def _summary_row(
    *,
    pair: str,
    interval: str,
    variant: str,
    rank_score: str,
    total_return_pct: str = "5",
    profit_factor: str = "1.5",
    trade_count: str = "5",
    risk: str = "1",
    leverage: str = "3",
) -> dict[str, object]:
    row = {field: "" for field in SUMMARY_FIELDS}
    row.update(
        {
            "pair": pair,
            "interval": interval,
            "variant": variant,
            "family": "hybrid_meta",
            "candles_loaded": "240",
            "rank_score": rank_score,
            "rank_grade": "B",
            "total_return_pct": total_return_pct,
            "profit_factor": profit_factor,
            "trade_count": trade_count,
            "risk_per_trade_pct": risk,
            "leverage": leverage,
            "take_profit_pct": "",
            "trailing_stop_enabled": "False",
            "trailing_stop_activation_pct": "0",
            "trailing_stop_distance_pct": "0",
            "trailing_profile": "off",
            "atr_dynamic_exits_enabled": "False",
            "error": "",
        }
    )
    return row


def _write_summary(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in SUMMARY_FIELDS})


class QuickValidatorTests(unittest.TestCase):
    def test_select_top_rows_uses_rank_score_and_skips_errors(self) -> None:
        rows = [
            _summary_row(pair="B-A_USDT", interval="5m", variant="hybrid_meta", rank_score="1"),
            _summary_row(pair="B-B_USDT", interval="5m", variant="hybrid_meta", rank_score="9"),
            {
                **_summary_row(
                    pair="B-C_USDT",
                    interval="5m",
                    variant="hybrid_meta",
                    rank_score="999",
                ),
                "error": "no candles",
            },
        ]

        selected = _select_top_rows(rows, limit=1)

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["pair"], "B-B_USDT")

    def test_run_quick_validator_writes_combined_outputs(self) -> None:
        variants = strategy_variants_for_names(("hybrid_meta", "adaptive_hybrid"))
        calls: list[object] = []

        def fake_run_research_sweep(*, settings, config, variants, logger=None):
            calls.append((config, variants))
            out = config.output_dir / "sweep_fake"
            summary = out / "summary.csv"
            if config.max_runs and config.max_runs > 1:
                rows = [
                    _summary_row(
                        pair="B-A_USDT",
                        interval="5m",
                        variant="hybrid_meta",
                        rank_score="10",
                        risk="1",
                        leverage="1",
                    ),
                    _summary_row(
                        pair="B-B_USDT",
                        interval="15m",
                        variant="adaptive_default",
                        rank_score="8",
                        risk="2",
                        leverage="3",
                    ),
                ]
            else:
                rows = [
                    _summary_row(
                        pair=config.pairs[0],
                        interval=config.intervals[0],
                        variant=variants[0].name,
                        rank_score=str(Decimal(config.lookback) / Decimal("10")),
                        risk=str(config.risk_per_trade_pct or ""),
                        leverage=str(config.leverage),
                    )
                ]
            _write_summary(summary, rows)
            return {
                "status": "finished",
                "summary_csv": str(summary),
                "workbook_xlsx": str(out / "backtest_results.xlsx"),
                "completed": len(rows),
                "errors": 0,
                "attempted": len(rows),
            }

        with TemporaryDirectory() as tmp:
            with patch(
                "app.research.quick_validator.run_research_sweep",
                side_effect=fake_run_research_sweep,
            ):
                result = run_quick_validator(
                    settings=Settings(),
                    variants=variants,
                    config=QuickValidationConfig(
                        base_sweep=SweepConfig(
                            pairs=("B-A_USDT", "B-B_USDT"),
                            intervals=("5m", "15m"),
                            risk_per_trade_pct_values=(Decimal("1"),),
                            leverage_values=(Decimal("1"),),
                            output_dir=Path(tmp),
                        ),
                        output_dir=Path(tmp),
                        quick_lookback=120,
                        validation_lookback=300,
                        final_lookback=500,
                        quick_max_runs=2,
                        top_validation=2,
                        top_final=1,
                    ),
                )

                self.assertEqual(len(calls), 4)
                self.assertTrue(Path(str(result["summary_csv"])).exists())
                self.assertTrue(Path(str(result["workbook_xlsx"])).exists())
                self.assertEqual(result["completed_rows"], 5)
                self.assertEqual(result["error_rows"], 0)


if __name__ == "__main__":
    unittest.main()
