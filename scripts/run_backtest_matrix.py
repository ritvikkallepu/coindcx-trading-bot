from __future__ import annotations

import argparse
import logging
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from app.backtest.engine import BacktestEngine
from app.backtest.models import BacktestConfig, BacktestResult
from app.config import Settings
from app.backtest.data_loader import load_historical_candle_series
from app.exchange.coindcx_rest import CoinDCXFuturesClient
from app.risk.manager import RiskManager
from app.strategies.defaults import strategy_engine_for_name

# Configure basic logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("matrix_runner")
logger.setLevel(logging.INFO)

# Suppress noisy logs from other modules
logging.getLogger("app.backtest.engine").setLevel(logging.WARNING)
logging.getLogger("app.broker.paper").setLevel(logging.WARNING)
logging.getLogger("app.strategies").setLevel(logging.WARNING)

# Input columns to parse
INPUT_COLUMNS = [
    "pair", "interval", "strategy", "lookback", "equity", "leverage",
    "risk_per_trade_pct", "max_daily_loss_pct", "stop_loss_pct", "take_profit_pct",
    "maker_fee_pct", "taker_fee_pct", "gst_on_fees_pct", "slippage_pct",
    "stop_slippage_pct", "trailing_stop_enabled", "trail_active_pct",
    "trail_distance_pct", "atr_dynamic_exits_enabled", "atr_mode",
    "atr_stop_multiple", "atr_trailing_multiple", "funding_rate_pct",
    "funding_interval_hours", "trade_quality_mode", "controlled_shorts_enabled"
]

# Result columns to write
RESULT_COLUMNS = [
    "status", "started_at", "completed_at", "error_message",
    "actual_candles_loaded", "candles_evaluated", "coverage_pct",
    "final_equity", "total_return_pct", "win_rate_pct", "profit_factor",
    "max_drawdown_pct", "trades", "long_entries", "short_entries",
    "exits", "fees_paid", "average_r", "largest_win", "largest_loss",
    "most_common_exit_reason", "most_common_block_reason", "run_quality",
    "candle_fetch_seconds", "backtest_seconds", "workbook_save_seconds", "total_row_seconds"
]

class CandleCache:
    def __init__(self):
        self.cache = {}

    def get_candles(self, client, pair, interval, lookback):
        key = (pair, interval, lookback)
        if key not in self.cache:
            logger.info(f"Fetching candles for {pair} {interval} (lookback={lookback})...")
            self.cache[key] = load_historical_candle_series(
                client=client,
                pair=pair,
                interval=interval,
                lookback=lookback,
            )
        else:
            logger.info(f"Using cached candles for {pair} {interval}.")
        return self.cache[key]

def parse_decimal(value: Any, default: Any = None) -> Decimal | None:
    if pd.isna(value) or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default

def parse_int(value: Any, default: Any = None) -> int | None:
    if pd.isna(value) or value == "":
        return default
    try:
        return int(value)
    except Exception:
        return default

def parse_bool(value: Any, default: bool = False) -> bool:
    if pd.isna(value) or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "on"}

import openpyxl

def run_matrix(args):
    workbook_path = Path(args.workbook)
    if not workbook_path.exists():
        logger.error(f"Workbook not found: {workbook_path}")
        return

    logger.info(f"Reading workbook: {workbook_path}")
    
    try:
        wb = openpyxl.load_workbook(workbook_path)
    except Exception as e:
        logger.error(f"Failed to open workbook (it might be corrupted or open in another program): {e}")
        return
        
    if "Backtest Queue" not in wb.sheetnames:
        logger.error("Sheet 'Backtest Queue' not found.")
        return
        
    ws = wb["Backtest Queue"]

    # Find header row
    header_row_idx = 1
    col_map = {}
    for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if not row: continue
        vals = [str(c).strip().lower() if c is not None else "" for c in row]
        if "pair" in vals and "interval" in vals:
            header_row_idx = row_idx
            for col_idx, val in enumerate(vals, start=1):
                if val:
                    col_map[val] = col_idx
            break
            
    if not col_map:
        logger.error("Could not find header row containing 'pair' and 'interval'.")
        return
        
    logger.info(f"Found headers on row {header_row_idx}: {list(col_map.keys())}")

    # Add missing result columns to the header
    max_col = ws.max_column
    for col_name in RESULT_COLUMNS:
        if col_name not in col_map:
            max_col += 1
            ws.cell(row=header_row_idx, column=max_col, value=col_name)
            col_map[col_name] = max_col

    # Save initial headers
    try:
        wb.save(workbook_path)
    except Exception as e:
        logger.error(f"Could not save workbook. Ensure it is closed. Error: {e}")
        return

    settings = Settings()
    client = CoinDCXFuturesClient()
    candle_cache = CandleCache()
    
    count = 0
    # Process rows after header
    for row_idx in range(header_row_idx + 1, ws.max_row + 1):
        if args.limit and count >= args.limit:
            logger.info(f"Limit of {args.limit} reached. Stopping.")
            break

        def get_val(col_name):
            if col_name in col_map:
                return ws.cell(row=row_idx, column=col_map[col_name]).value
            return None

        def set_val(col_name, value):
            if col_name in col_map:
                ws.cell(row=row_idx, column=col_map[col_name], value=value)

        pair_val = get_val("pair")
        if not pair_val: # Empty row
            continue
            
        pair_row = str(pair_val)
        if args.coin and args.coin not in pair_row:
            continue

        status = str(get_val("status") or "").strip().lower()
        
        if status == "complete":
            continue
        if status == "failed" and not args.retry_failed:
            continue
            
        logger.info(f"Processing row {row_idx}: {pair_row} {get_val('interval')} {get_val('strategy')}")

        if args.dry_run:
            logger.info(f"[DRY-RUN] Would process row {row_idx}")
            continue

        # Mark as Running
        set_val("status", "Running")
        set_val("started_at", datetime.now(timezone.utc).isoformat())
        wb.save(workbook_path)

        try:
            pair = str(get_val("pair"))
            interval = str(get_val("interval"))
            strategy = str(get_val("strategy"))
            lookback = parse_int(get_val("lookback"), 1000)
            equity = parse_decimal(get_val("equity"), Decimal("1000"))
            leverage = parse_decimal(get_val("leverage"), Decimal("3"))
            
            risk_pct = parse_decimal(get_val("risk_per_trade_pct"))
            max_loss_pct = parse_decimal(get_val("max_daily_loss_pct"), Decimal("10"))
            
            stop_loss_pct = parse_decimal(get_val("stop_loss_pct"))
            take_profit_pct = parse_decimal(get_val("take_profit_pct"))
            
            maker_fee = parse_decimal(get_val("maker_fee_pct"), Decimal("0.02")) / 100
            taker_fee = parse_decimal(get_val("taker_fee_pct"), Decimal("0.05")) / 100
            fee_gst = parse_decimal(get_val("gst_on_fees_pct"), Decimal("18")) / 100
            
            slippage = parse_decimal(get_val("slippage_pct"), Decimal("0.02"))
            stop_slippage = parse_decimal(get_val("stop_slippage_pct"), Decimal("0.02"))
            
            trailing_enabled = parse_bool(get_val("trailing_stop_enabled"), False)
            trail_active = parse_decimal(get_val("trail_active_pct"), Decimal("1"))
            trail_distance = parse_decimal(get_val("trail_distance_pct"), Decimal("2"))
            
            atr_enabled = parse_bool(get_val("atr_dynamic_exits_enabled"), False)
            atr_mode = str(get_val("atr_mode") or "router")
            atr_stop_mult = parse_decimal(get_val("atr_stop_multiple"), Decimal("1.5"))
            atr_trailing_mult = parse_decimal(get_val("atr_trailing_multiple"), Decimal("2.0"))
            
            funding_rate = parse_decimal(get_val("funding_rate_pct"), Decimal("0")) / 100
            funding_interval = parse_int(get_val("funding_interval_hours"), 8)
            
            quality_mode = str(get_val("trade_quality_mode") or "strict")
            controlled_shorts = parse_bool(get_val("controlled_shorts_enabled"), False)

            config = BacktestConfig(
                pair=pair,
                interval=interval,
                starting_equity=equity,
                leverage=leverage,
                strategy_name=strategy,
                requested_candles=lookback,
                risk_per_trade_pct=risk_pct,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                maker_fee_rate=maker_fee,
                taker_fee_rate=taker_fee,
                fee_gst_rate=fee_gst,
                slippage_pct=slippage,
                stop_slippage_pct=stop_slippage,
                trailing_stop_enabled=trailing_enabled,
                trailing_stop_activation_pct=trail_active,
                trailing_stop_distance_pct=trail_distance,
                atr_dynamic_exits_enabled=atr_enabled,
                atr_policy_mode=atr_mode,
                atr_stop_multiple=atr_stop_mult,
                atr_trailing_multiple=atr_trailing_mult,
                funding_fee_rate=funding_rate,
                funding_interval_hours=funding_interval,
                trade_quality_mode=quality_mode,
                controlled_shorts_enabled=controlled_shorts,
                max_daily_loss_pct=max_loss_pct,
            )

            # Load candles
            series = candle_cache.get_candles(client, pair, interval, lookback)
            if not series:
                raise ValueError(f"No candles found for {pair}")

            # Run
            engine = BacktestEngine(
                config=config,
                strategy_engine=strategy_engine_for_name(strategy),
                risk_manager=RiskManager(
                    replace(
                        settings.risk,
                        max_risk_per_trade_pct=risk_pct if risk_pct else settings.risk.max_risk_per_trade_pct,
                        max_daily_loss_pct=max_loss_pct,
                        trailing_stop_enabled=trailing_enabled,
                        trailing_stop_activation_pct=trail_active,
                        trailing_stop_distance_pct=trail_distance,
                    )
                ),
            )
            
            result = engine.run(series)
            
            metrics = result.metrics
            summary = result.to_dict(recent_count=0)
            funnel = summary.get("signal_funnel", {})
            diagnostics = summary.get("diagnostics", {})
            
            set_val("actual_candles_loaded", result.candles_loaded)
            set_val("candles_evaluated", result.candles_used)
            
            coverage = (result.candles_loaded / lookback * 100) if lookback and lookback > 0 else 0
            set_val("coverage_pct", round(coverage, 2))
            
            set_val("final_equity", float(metrics.final_equity))
            set_val("total_return_pct", float(metrics.total_return_pct))
            set_val("win_rate_pct", float(metrics.win_rate_pct) if metrics.win_rate_pct is not None else 0)
            set_val("profit_factor", float(metrics.profit_factor) if metrics.profit_factor is not None else 0)
            set_val("max_drawdown_pct", float(metrics.max_drawdown_pct))
            
            set_val("trades", len(result.trades))
            
            actions = diagnostics.get("signal_action_counts", {})
            set_val("long_entries", actions.get("enter_long", 0))
            set_val("short_entries", actions.get("enter_short", 0))
            set_val("exits", actions.get("exit_long", 0) + actions.get("exit_short", 0))
            set_val("fees_paid", float(metrics.fees_paid))
            
            set_val("average_r", float(diagnostics.get("average_r_multiple", 0) or 0))
            
            if result.trades:
                pnl_list = [float(t.net_pnl) for t in result.trades]
                set_val("largest_win", max(pnl_list))
                set_val("largest_loss", min(pnl_list))
            
            exit_reasons = diagnostics.get("exit_reasons", {})
            if exit_reasons:
                top_exit = sorted(exit_reasons.items(), key=lambda x: x[1], reverse=True)[0][0]
                set_val("most_common_exit_reason", top_exit)
            
            block_reasons = funnel.get("block_reasons", {})
            if block_reasons:
                active_blocks = {k: v for k, v in block_reasons.items() if v > 0 and k != "executed"}
                if active_blocks:
                    top_block = sorted(active_blocks.items(), key=lambda x: x[1], reverse=True)[0][0]
                    set_val("most_common_block_reason", top_block)

            from app.dashboard.state import classify_run_quality
            quality = classify_run_quality(result)
            set_val("run_quality", quality.get("label", "Unknown"))

            set_val("status", "Complete")
            set_val("completed_at", datetime.now(timezone.utc).isoformat())
            set_val("error_message", "")

        except Exception as e:
            logger.exception(f"Error processing row {row_idx}")
            set_val("status", "Failed")
            set_val("error_message", str(e))
            set_val("completed_at", datetime.now(timezone.utc).isoformat())

        wb.save(workbook_path)
        count += 1
        
        if args.sleep > 0:
            time.sleep(args.sleep)

    logger.info("Batch run complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Headless Batch Backtest Runner")
    parser.add_argument("--workbook", required=True, help="Path to the Excel matrix")
    parser.add_argument("--resume", action="store_true", help="Resume from last pending row")
    parser.add_argument("--limit", type=int, help="Limit number of rows to process")
    parser.add_argument("--coin", help="Only run for rows containing this coin symbol")
    parser.add_argument("--dry-run", action="store_true", help="Do not execute backtests")
    parser.add_argument("--retry-failed", action="store_true", help="Retry rows marked as Failed")
    parser.add_argument("--sleep", type=float, default=0.5, help="Sleep between runs (seconds)")
    
    args = parser.parse_args()
    run_matrix(args)
