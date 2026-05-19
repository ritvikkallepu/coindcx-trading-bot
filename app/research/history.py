from __future__ import annotations

import csv
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.backtest.models import BacktestResult
from app.fees import effective_fee_rate
from app.risk.models import convert_for_json
from app.research.excel import write_sweep_workbook


DEFAULT_HISTORY_DIR = Path("research/backtests/history")
HISTORY_FIELDS = (
    "recorded_at",
    "source",
    "pair",
    "interval",
    "variant",
    "family",
    "execution_interval",
    "intrabar_reentry_enabled",
    "max_reentries_per_candle",
    "reentry_cooldown_candles",
    "stop_loss_cooldown_candles",
    "max_consecutive_losses",
    "loss_cooldown_candles",
    "lookback_candles",
    "candles_loaded",
    "candles_used",
    "starting_equity",
    "leverage",
    "risk_per_trade_pct",
    "compound_risk_equity",
    "stop_loss_pct",
    "take_profit_pct",
    "fee_rate",
    "maker_fee_rate",
    "taker_fee_rate",
    "fee_gst_rate",
    "maker_fee_effective_rate",
    "taker_fee_effective_rate",
    "entry_fee_type",
    "exit_fee_type",
    "slippage_pct",
    "stop_slippage_pct",
    "funding_fee_rate",
    "funding_interval_hours",
    "trailing_stop_enabled",
    "trailing_stop_activation_pct",
    "trailing_stop_distance_pct",
    "atr_dynamic_exits_enabled",
    "atr_stop_enabled",
    "atr_take_profit_enabled",
    "atr_trailing_enabled",
    "atr_entry_filter_enabled",
    "atr_policy_mode",
    "atr_period",
    "atr_stop_multiple",
    "atr_take_profit_multiple",
    "atr_take_profit_mode",
    "total_return_pct",
    "final_equity",
    "net_pnl",
    "trade_count",
    "win_rate_pct",
    "profit_factor",
    "max_drawdown_pct",
    "sharpe_ratio",
    "fees_paid",
    "funding_paid",
    "accepted_reports",
    "rejected_reports",
    "enter_long",
    "enter_short",
    "exit_long",
    "exit_short",
    "hold",
    "error",
)

_HISTORY_LOCK = threading.Lock()


def record_backtest_result(
    result: BacktestResult,
    *,
    source: str,
    history_dir: Path = DEFAULT_HISTORY_DIR,
) -> dict[str, Any]:
    with _HISTORY_LOCK:
        history_dir.mkdir(parents=True, exist_ok=True)
        csv_path = history_dir / "backtest_history.csv"
        jsonl_path = history_dir / "backtest_history.jsonl"
        workbook_path = history_dir / "backtest_history.xlsx"

        rows = _read_history_rows(csv_path)
        row = backtest_history_row(result, source=source)
        rows.append(row)

        _write_history_csv(csv_path, rows)
        _append_jsonl(jsonl_path, row)
        workbook_result = _write_history_workbook(
            workbook_path=workbook_path,
            rows=rows,
            source_count=len(rows),
        )

        return {
            "saved": True,
            "rows": len(rows),
            "csv": str(csv_path),
            "jsonl": str(jsonl_path),
            **workbook_result,
        }


def backtest_history_row(result: BacktestResult, *, source: str) -> dict[str, object]:
    summary = result.to_dict(recent_count=0)
    diagnostics = summary["diagnostics"]
    actions = diagnostics["signal_action_counts"]
    metrics = result.metrics
    config = result.config
    return {
        "recorded_at": datetime.now().astimezone().isoformat(),
        "source": source,
        "pair": config.pair,
        "interval": config.interval,
        "variant": config.strategy_name,
        "family": _strategy_family(config.strategy_name),
        "execution_interval": config.execution_interval,
        "intrabar_reentry_enabled": config.intrabar_reentry_enabled,
        "max_reentries_per_candle": config.max_reentries_per_candle,
        "reentry_cooldown_candles": config.reentry_cooldown_candles,
        "stop_loss_cooldown_candles": config.stop_loss_cooldown_candles,
        "max_consecutive_losses": config.max_consecutive_losses,
        "loss_cooldown_candles": config.loss_cooldown_candles,
        "lookback_candles": result.candles_loaded,
        "candles_loaded": result.candles_loaded,
        "candles_used": result.candles_used,
        "starting_equity": config.starting_equity,
        "leverage": config.leverage,
        "risk_per_trade_pct": config.risk_per_trade_pct,
        "compound_risk_equity": config.compound_risk_equity,
        "stop_loss_pct": config.stop_loss_pct,
        "take_profit_pct": config.take_profit_pct,
        "fee_rate": config.taker_fee_rate,
        "maker_fee_rate": config.maker_fee_rate,
        "taker_fee_rate": config.taker_fee_rate,
        "fee_gst_rate": config.fee_gst_rate,
        "maker_fee_effective_rate": effective_fee_rate(
            config.maker_fee_rate, config.fee_gst_rate
        ),
        "taker_fee_effective_rate": effective_fee_rate(
            config.taker_fee_rate, config.fee_gst_rate
        ),
        "entry_fee_type": config.entry_fee_type,
        "exit_fee_type": config.exit_fee_type,
        "slippage_pct": config.slippage_pct,
        "stop_slippage_pct": config.stop_slippage_pct,
        "funding_fee_rate": config.funding_fee_rate,
        "funding_interval_hours": config.funding_interval_hours,
        "trailing_stop_enabled": config.trailing_stop_enabled,
        "trailing_stop_activation_pct": config.trailing_stop_activation_pct,
        "trailing_stop_distance_pct": config.trailing_stop_distance_pct,
        "atr_dynamic_exits_enabled": config.atr_dynamic_exits_enabled,
        "atr_stop_enabled": config.atr_stop_enabled,
        "atr_take_profit_enabled": config.atr_take_profit_enabled,
        "atr_trailing_enabled": config.atr_trailing_enabled,
        "atr_entry_filter_enabled": config.atr_entry_filter_enabled,
        "atr_policy_mode": config.atr_policy_mode,
        "atr_period": config.atr_period,
        "atr_stop_multiple": config.atr_stop_multiple,
        "atr_take_profit_multiple": config.atr_take_profit_multiple,
        "atr_take_profit_mode": config.atr_take_profit_mode,
        "total_return_pct": metrics.total_return_pct,
        "final_equity": metrics.final_equity,
        "net_pnl": metrics.net_pnl,
        "trade_count": metrics.trade_count,
        "win_rate_pct": metrics.win_rate_pct,
        "profit_factor": metrics.profit_factor,
        "max_drawdown_pct": metrics.max_drawdown_pct,
        "sharpe_ratio": metrics.sharpe_ratio,
        "fees_paid": metrics.fees_paid,
        "funding_paid": metrics.funding_paid,
        "accepted_reports": summary["accepted_report_count"],
        "rejected_reports": summary["rejected_report_count"],
        "enter_long": actions.get("enter_long", 0),
        "enter_short": actions.get("enter_short", 0),
        "exit_long": actions.get("exit_long", 0),
        "exit_short": actions.get("exit_short", 0),
        "hold": actions.get("hold", 0),
        "error": "",
    }


def _read_history_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return [_normalize_row(row) for row in csv.DictReader(handle) if row]


def _normalize_row(row: dict[str, object]) -> dict[str, object]:
    return {field: row.get(field, "") for field in HISTORY_FIELDS}


def _write_history_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(convert_for_json(_normalize_row(row)))


def _append_jsonl(path: Path, row: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(convert_for_json(row), sort_keys=True) + "\n")


def _write_history_workbook(
    *,
    workbook_path: Path,
    rows: list[dict[str, object]],
    source_count: int,
) -> dict[str, Any]:
    assumptions = {
        "status": "updated",
        "pairs": "manual dashboard/CLI history",
        "intervals": "all manual backtests",
        "completed_backtests": source_count,
        "notes": (
            "Auto-updated by CLI and dashboard backtests. "
            "If the main workbook is open in Excel, a stable latest copy is written instead."
        ),
    }
    temp_path = workbook_path.with_suffix(".tmp.xlsx")
    try:
        write_sweep_workbook(temp_path, rows=rows, assumptions=assumptions)
        temp_path.replace(workbook_path)
        return {
            "workbook_xlsx": str(workbook_path),
            "workbook_update_status": "main_updated",
        }
    except OSError as exc:
        fallback = workbook_path.with_name("backtest_history_latest.xlsx")
        try:
            if temp_path.exists():
                temp_path.replace(fallback)
            else:
                write_sweep_workbook(fallback, rows=rows, assumptions=assumptions)
        except OSError:
            return {
                "workbook_xlsx": str(workbook_path),
                "workbook_error": str(exc),
            }
        return {
            "workbook_xlsx": str(workbook_path),
            "fallback_workbook_xlsx": str(fallback),
            "workbook_update_status": "main_locked_latest_copy_updated",
            "workbook_error": str(exc),
        }


def _strategy_family(strategy_name: str) -> str:
    if strategy_name.startswith("adaptive") or strategy_name == "adaptive_hybrid":
        return "adaptive_hybrid"
    if strategy_name.startswith("ema") or strategy_name == "ema_rsi_trend":
        return "ema_rsi_trend"
    if strategy_name.startswith("grid") or strategy_name == "bb_dynamic_grid":
        return "bb_dynamic_grid"
    if strategy_name.startswith("bb") or strategy_name == "bb_volume_reversion":
        return "bb_volume_reversion"
    if strategy_name.startswith("hybrid_meta"):
        return "hybrid_meta"
    return strategy_name
