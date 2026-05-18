from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.backtest.models import BacktestEquityPoint, BacktestResult, BacktestTrade
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
    atr_take_profit_mode: str = "none"
    execution_interval: str = ""
    intrabar_reentry_enabled: bool = True
    max_reentries_per_candle: int = 1
    reentry_cooldown_candles: int = 1
    stop_loss_cooldown_candles: int = 1
    max_consecutive_losses: int = 2
    loss_cooldown_candles: int = 4
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

    return_pct = metrics.total_return_pct
    drawdown_pct = metrics.max_drawdown_pct
    profit_factor = metrics.profit_factor or Decimal("0")

    if return_pct >= Decimal("8") and profit_factor >= Decimal("1.5") and drawdown_pct <= Decimal("5"):
        return {
            "label": "Strong Paper Run",
            "tone": "good",
            "detail": "Positive return, healthy profit factor, and controlled drawdown.",
        }
    if return_pct > 0 and profit_factor >= Decimal("1.1"):
        return {
            "label": "Watchlist",
            "tone": "warn",
            "detail": "Positive result, but needs more validation before paper automation.",
        }
    return {
        "label": "Weak Run",
        "tone": "bad",
        "detail": "Result does not pass the paper-quality filter.",
    }
