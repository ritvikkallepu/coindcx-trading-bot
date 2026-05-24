from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.backtest.models import (
    BacktestEquityPoint,
    BacktestResult,
    BacktestTrade,
    backtest_report_diagnostics,
)
from app.config import Settings
from app.fees import (
    COINDCX_FEE_GST_RATE,
    COINDCX_INR_M_MAKER_FEE_RATE,
    COINDCX_INR_M_TAKER_FEE_RATE,
)
from app.risk.models import convert_for_json


@dataclass(frozen=True)
class DashboardDefaults:
    pair: str = "B-SOL_USDT"
    interval: str = "1h"
    lookback: int = 1000
    equity: Decimal = Decimal("1000")
    leverage: Decimal = Decimal("3")
    risk_per_trade_pct: Decimal = Decimal("5")
    compound_risk_equity: bool = False
    stop_loss_pct: Decimal | None = None
    take_profit_pct: Decimal | None = None
    maker_fee_rate: Decimal = COINDCX_INR_M_MAKER_FEE_RATE
    taker_fee_rate: Decimal = COINDCX_INR_M_TAKER_FEE_RATE
    fee_gst_rate: Decimal = COINDCX_FEE_GST_RATE
    entry_fee_type: str = "maker"
    exit_fee_type: str = "taker"
    slippage_pct: Decimal = Decimal("0.02")
    stop_slippage_pct: Decimal = Decimal("0.02")
    funding_fee_rate: Decimal = Decimal("0")
    funding_interval_hours: int = 8
    trailing_stop_enabled: bool = False
    trailing_stop_activation_pct: Decimal = Decimal("1")
    trailing_stop_distance_pct: Decimal = Decimal("2")
    atr_dynamic_exits_enabled: bool = False
    atr_stop_enabled: bool = True
    atr_take_profit_enabled: bool = False
    atr_trailing_enabled: bool = True
    atr_entry_filter_enabled: bool = True
    atr_policy_mode: str = "router"
    atr_period: int = 14
    atr_stop_multiple: Decimal = Decimal("1.5")
    atr_take_profit_multiple: Decimal = Decimal("3")
    atr_trailing_multiple: Decimal = Decimal("2.0")
    atr_take_profit_mode: str = "none"
    execution_interval: str = ""
    intrabar_reentry_enabled: bool = True
    max_reentries_per_candle: int = 1
    reentry_cooldown_candles: int = 1
    stop_loss_cooldown_candles: int = 1
    max_consecutive_losses: int = 2
    loss_cooldown_candles: int = 4
    max_daily_loss_pct: Decimal = Decimal("10")
    
    # Task 2: Equity giveback guard
    equity_giveback_guard_enabled: bool = False
    equity_giveback_threshold_pct: Decimal = Decimal("0.035")
    equity_giveback_cooldown_candles: int = 72
    
    # Task 3: Loss-streak cooldown enhancements
    loss_streak_cooldown_enabled: bool = False
    consecutive_loss_limit: int = 3
    loss_streak_cooldown_candles: int = 12
    rolling_loss_window: int = 8
    rolling_loss_limit: int = 5
    rolling_loss_cooldown_candles: int = 36
    
    # Task 4: Post-spike cooldown
    post_spike_cooldown_enabled: bool = False
    post_spike_lookback_candles: int = 50
    post_spike_gain_threshold_pct: Decimal = Decimal("0.05")
    post_spike_cooldown_candles: int = 24
    
    # Task 5: Breakeven and profit-lock
    breakeven_enabled: bool = False
    breakeven_activation_r: Decimal = Decimal("1.0")
    breakeven_offset_r: Decimal = Decimal("0")
    profit_lock_enabled: bool = False
    profit_lock_activation_r: Decimal = Decimal("1.5")
    profit_lock_r: Decimal = Decimal("0.5")
    atr_trail_after_r_enabled: bool = False
    atr_trail_activation_r: Decimal = Decimal("2.0")
    
    # Task 6: Chop/regime filter
    chop_filter_enabled: bool = False
    min_ema_gap_pct: Decimal = Decimal("0.0015")
    min_atr_pct: Decimal = Decimal("0.002")
    block_flat_ema_enabled: bool = False
    block_low_atr_enabled: bool = False
    
    strategy: str = "bb_dynamic_grid"

    def to_dict(self) -> dict[str, Any]:
        return convert_for_json(asdict(self))


def build_status_payload(
    *,
    settings: Settings,
    defaults: DashboardDefaults,
) -> dict[str, Any]:
    return {
        "generated_at": _now_iso(),
        "bot": {
            "status": "paper_ready",
            "mode": settings.trading_mode,
            "live_trading_allowed": settings.live_trading_allowed,
            "live_trading_enabled": settings.live_trading_enabled,
            "default_pair": settings.default_pair,
            "futures_margin_currency": settings.futures_margin_currency,
            "price_quote_currency": settings.price_quote_currency,
            "quote_to_margin_rate": settings.quote_to_margin_rate,
        },
        "safety": {
            "live_orders_locked": not settings.live_trading_allowed,
            "futures_only": True,
            "private_mutations_enabled": settings.live_trading_allowed,
            "paper_execution_available": True,
            "backtesting_available": True,
        },
        "risk": convert_for_json(asdict(settings.risk)),
        "defaults": defaults.to_dict(),
        "strategy_profile": build_strategy_profile(
            strategy=defaults.strategy,
            interval=defaults.interval,
        ),
        "modules": [
            {"name": "Config", "status": "ready"},
            {"name": "Public Data", "status": "ready"},
            {"name": "Strategies", "status": "research"},
            {"name": "Risk Manager", "status": "paper"},
            {"name": "Paper Broker", "status": "ready"},
            {"name": "Backtesting", "status": "ready"},
            {"name": "Live Execution", "status": "locked"},
        ],
    }


def build_backtest_dashboard_payload(
    result: BacktestResult,
    *,
    recent_count: int = 20,
    max_equity_points: int = 240,
) -> dict[str, Any]:
    summary = result.to_dict(recent_count=recent_count)
    return {
        "generated_at": _now_iso(),
        "summary": summary,
        "config": result.config.to_dict(),
        "metrics": result.metrics.to_dict(),
        "final_account": result.final_account.to_dict(),
        "equity_curve": [
            point.to_dict()
            for point in downsample_equity_curve(
                result.equity_curve,
                max_points=max_equity_points,
            )
        ],
        "recent_trades": summary["recent_trades"],
        "trades": [trade.to_dict() for trade in result.trades],
        "trade_timeline": [
            _trade_timeline_row(index=index, trade=trade)
            for index, trade in enumerate(result.trades, start=1)
        ],
        "recent_orders": summary["recent_orders"],
        "recent_fills": summary["recent_fills"],
        "counts": {
            "candles_loaded": result.candles_loaded,
            "candles_used": result.candles_used,
            "trades": len(result.trades),
            "orders": len(result.orders),
            "fills": len(result.fills),
            "accepted_reports": summary["accepted_report_count"],
            "rejected_reports": summary["rejected_report_count"],
        },
        "diagnostics": summary["diagnostics"],
        "signal_funnel": summary["signal_funnel"],
        "strategy_profile": build_strategy_profile(
            strategy=result.config.strategy_name,
            interval=result.config.interval,
        ),
        "run_quality": classify_run_quality(result),
    }


def _trade_timeline_row(*, index: int, trade: BacktestTrade) -> dict[str, Any]:
    return {
        **trade.to_dict(),
        "trade_number": index,
        "entry_time": _format_timestamp_ms(trade.entry_time_ms),
        "exit_time": _format_timestamp_ms(trade.exit_time_ms),
        "duration": _format_duration_ms(trade.exit_time_ms - trade.entry_time_ms),
    }


def _format_timestamp_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _format_duration_ms(duration_ms: int) -> str:
    if duration_ms <= 0:
        return "0m"
    total_minutes = duration_ms // 60_000
    days, remainder = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def downsample_equity_curve(
    points: list[BacktestEquityPoint],
    *,
    max_points: int,
) -> list[BacktestEquityPoint]:
    if max_points <= 0:
        return []
    if len(points) <= max_points:
        return list(points)
    if max_points == 1:
        return [points[-1]]

    last_index = len(points) - 1
    selected: list[BacktestEquityPoint] = []
    seen: set[int] = set()
    for output_index in range(max_points):
        raw_index = round((output_index * last_index) / (max_points - 1))
        if raw_index not in seen:
            selected.append(points[raw_index])
            seen.add(raw_index)
    if selected[-1] != points[-1]:
        selected[-1] = points[-1]
    return selected


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_strategy_profile(*, strategy: str, interval: str) -> dict[str, str]:
    normalized_strategy = strategy.strip().lower()

    if normalized_strategy == "adaptive_hybrid":
        return {
            "name": "adaptive_hybrid",
            "label": "Adaptive Hybrid",
            "mode": "Market Adaptive",
            "primary": "Regime selector",
            "secondary": "EMA-RSI / Bollinger",
            "filter": "Visual + OI",
            "status": "Paper Research",
        }

    if normalized_strategy == "hybrid_meta":
        return {
            "name": "hybrid_meta",
            "label": "Weighted Hybrid",
            "mode": "Score Blend",
            "primary": "EMA + BB + Visual",
            "secondary": "OI optional",
            "filter": "Visual screen",
            "status": "Experimental",
        }

    if normalized_strategy == "hybrid_meta_v2":
        return {
            "name": "hybrid_meta_v2",
            "label": "Weighted Hybrid V2",
            "mode": "Score Blend V2",
            "primary": "EMA + BB + Visual",
            "secondary": "OI only when scored",
            "filter": "Entry-only visual screen",
            "status": "Paper Research",
        }

    if normalized_strategy == "ema_rsi_trend":
        return {
            "name": "ema_rsi_trend",
            "label": "EMA-RSI Trend",
            "mode": "Trend",
            "primary": "EMA crossover",
            "secondary": "RSI filter",
            "filter": "ATR exits",
            "status": "Baseline",
        }

    if normalized_strategy == "bb_volume_reversion":
        return {
            "name": "bb_volume_reversion",
            "label": "BB Reversion",
            "mode": "Mean Reversion",
            "primary": "Bollinger squeeze",
            "secondary": "Volume confirmation",
            "filter": "ATR stop",
            "status": "Baseline",
        }

    if normalized_strategy == "bb_dynamic_grid":
        return {
            "name": "bb_dynamic_grid",
            "label": "BB Futures Grid",
            "mode": "Futures Grid",
            "primary": "Bollinger range",
            "secondary": "Basket scale-ins",
            "filter": "Close-confirmed trail",
            "status": "Paper Research",
        }

    return {
        "name": normalized_strategy or "unknown",
        "label": strategy or "Unknown",
        "mode": "Mixed",
        "primary": "Multiple",
        "secondary": "Multiple",
        "filter": "Risk layer",
        "status": "Research",
    }


def classify_run_quality(result: BacktestResult) -> dict[str, str]:
    metrics = result.metrics
    if metrics.trade_count <= 0:
        return {
            "label": "No Trades",
            "tone": "warn",
            "detail": "No closed trades in this backtest window.",
        }
    if metrics.trade_count < 10:
        return {
            "label": "Insignificant",
            "tone": "warn",
            "detail": f"Too few trades ({metrics.trade_count}) for statistical significance.",
        }

    return_pct = metrics.total_return_pct
    drawdown_pct = metrics.max_drawdown_pct
    profit_factor = metrics.profit_factor or Decimal("0")
    
    # Advanced diagnostics for penalty - using raw objects to avoid string conversion issues
    diag = backtest_report_diagnostics(result.reports, result.trades, result.equity_curve)
    dependency_pct = diag.get("dependency", {}).get("top_5_dependency_pct", Decimal("0"))
    thirds = diag.get("thirds_pnl", {})
    one_regime_result = False
    if thirds:
        vals = [abs(v) for v in thirds.values()]
        if max(vals) > 0 and (sum(1 for v in thirds.values() if v > 0) <= 1):
             one_regime_result = True # Profit coming from only one third

    # Return/DD ratio
    ret_dd_ratio = return_pct / drawdown_pct if drawdown_pct > 0 else return_pct

    if (
        return_pct >= Decimal("10") 
        and profit_factor >= Decimal("1.5") 
        and drawdown_pct <= Decimal("6")
        and dependency_pct < Decimal("40")
        and not one_regime_result
    ):
        return {
            "label": "Strong Approved",
            "tone": "good",
            "detail": "Robust multi-regime performance with controlled dependency.",
        }
        
    if (
        return_pct >= Decimal("5") 
        and profit_factor >= Decimal("1.25") 
        and ret_dd_ratio >= Decimal("0.8")
        and dependency_pct < Decimal("60")
    ):
        return {
            "label": "Watchlist+",
            "tone": "good",
            "detail": "Healthy metrics, suitable for careful paper testing.",
        }

    if return_pct > 0 and profit_factor >= Decimal("1.1"):
        return {
            "label": "Watchlist",
            "tone": "warn",
            "detail": "Positive result, but fragile or too dependent on few trades.",
        }
        
    return {
        "label": "Weak Run",
        "tone": "bad",
        "detail": "Result does not pass the quality filter (low PF, high DD, or high dependency).",
    }
