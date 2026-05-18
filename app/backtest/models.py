from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from app.broker.models import (
    PaperAccountSnapshot,
    PaperExecutionReport,
    PaperFill,
    PaperOrder,
)
from app.risk.models import convert_for_json
from app.strategies.base import SignalDirection


@dataclass(frozen=True)
class BacktestConfig:
    pair: str
    interval: str
    starting_equity: Decimal
    leverage: Decimal
    strategy_name: str = "all"
    requested_candles: int | None = None
    risk_per_trade_pct: Decimal | None = None
    compound_risk_equity: bool = False
    trade_quality_mode: str = "strict"
    controlled_shorts_enabled: bool = False
    a_setup_score_threshold: Decimal = Decimal("0.50")
    a_setup_agreement_threshold: Decimal = Decimal("0.65")
    b_setup_score_threshold: Decimal = Decimal("0.40")
    b_setup_agreement_threshold: Decimal = Decimal("0.55")
    b_setup_risk_multiplier: Decimal = Decimal("0.50")
    minimum_visual_score: Decimal = Decimal("0")
    long_entry_threshold: Decimal = Decimal("0.40")
    short_entry_threshold: Decimal = Decimal("0.55")
    short_agreement_threshold: Decimal = Decimal("0.60")
    stop_loss_pct: Decimal | None = None
    take_profit_pct: Decimal | None = None
    maker_fee_rate: Decimal = Decimal("0")
    taker_fee_rate: Decimal = Decimal("0")
    fee_gst_rate: Decimal = Decimal("0")
    entry_fee_type: str = "maker"
    exit_fee_type: str = "taker"
    slippage_pct: Decimal = Decimal("0")
    stop_slippage_pct: Decimal | None = None
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
    execution_interval: str | None = None
    intrabar_reentry_enabled: bool = False
    max_reentries_per_candle: int = 0
    reentry_cooldown_candles: int = 1
    stop_loss_cooldown_candles: int = 1
    max_consecutive_losses: int = 2
    loss_cooldown_candles: int = 4

    def __post_init__(self) -> None:
        if self.requested_candles is not None and self.requested_candles <= 0:
            raise ValueError("requested_candles must be positive when provided.")
        if self.trade_quality_mode not in {"strict", "loose", "tiered"}:
            raise ValueError("trade_quality_mode must be strict, loose, or tiered.")
        if self.a_setup_score_threshold <= 0:
            raise ValueError("a_setup_score_threshold must be positive.")
        if self.a_setup_agreement_threshold <= 0:
            raise ValueError("a_setup_agreement_threshold must be positive.")
        if self.b_setup_score_threshold <= 0:
            raise ValueError("b_setup_score_threshold must be positive.")
        if self.b_setup_agreement_threshold <= 0:
            raise ValueError("b_setup_agreement_threshold must be positive.")
        if self.b_setup_risk_multiplier <= 0 or self.b_setup_risk_multiplier > 1:
            raise ValueError("b_setup_risk_multiplier must be in the range (0, 1].")
        if self.long_entry_threshold <= 0:
            raise ValueError("long_entry_threshold must be positive.")
        if self.short_entry_threshold <= 0:
            raise ValueError("short_entry_threshold must be positive.")
        if self.short_agreement_threshold <= 0:
            raise ValueError("short_agreement_threshold must be positive.")
        if self.stop_loss_pct is not None and self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be positive when provided.")
        if self.take_profit_pct is not None and self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct must be positive when provided.")
        if self.fee_gst_rate < 0:
            raise ValueError("fee_gst_rate cannot be negative.")
        if self.stop_slippage_pct is not None and self.stop_slippage_pct < 0:
            raise ValueError("stop_slippage_pct cannot be negative.")
        if self.funding_interval_hours <= 0:
            raise ValueError("funding_interval_hours must be positive.")
        if self.atr_period <= 0:
            raise ValueError("atr_period must be positive.")
        if self.atr_stop_multiple <= 0:
            raise ValueError("atr_stop_multiple must be positive.")
        if self.atr_take_profit_multiple <= 0:
            raise ValueError("atr_take_profit_multiple must be positive.")
        if self.max_reentries_per_candle < 0:
            raise ValueError("max_reentries_per_candle cannot be negative.")
        if self.reentry_cooldown_candles < 0:
            raise ValueError("reentry_cooldown_candles cannot be negative.")
        if self.stop_loss_cooldown_candles < 0:
            raise ValueError("stop_loss_cooldown_candles cannot be negative.")
        if self.max_consecutive_losses < 0:
            raise ValueError("max_consecutive_losses cannot be negative.")
        if self.loss_cooldown_candles < 0:
            raise ValueError("loss_cooldown_candles cannot be negative.")
        if self.atr_take_profit_mode not in {
            "fixed",
            "entry_atr",
            "ratchet",
            "trailing_atr",
            "none",
        }:
            raise ValueError(
                "atr_take_profit_mode must be fixed, entry_atr, ratchet, trailing_atr, or none."
            )
        if self.atr_policy_mode not in {"manual", "router"}:
            raise ValueError("atr_policy_mode must be manual or router.")

    def to_dict(self) -> dict[str, Any]:
        return convert_for_json(asdict(self))


@dataclass(frozen=True)
class BacktestEquityPoint:
    timestamp_ms: int
    close_price: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    open_position_count: int
    open_notional: Decimal
    funding_paid: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return convert_for_json(asdict(self))


@dataclass(frozen=True)
class BacktestTrade:
    pair: str
    strategy_name: str
    direction: SignalDirection
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    entry_time_ms: int
    exit_time_ms: int
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    exit_reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def won(self) -> bool:
        return self.net_pnl > 0

    def to_dict(self) -> dict[str, Any]:
        return convert_for_json(asdict(self))


@dataclass(frozen=True)
class BacktestMetrics:
    starting_equity: Decimal
    final_equity: Decimal
    total_return: Decimal
    total_return_pct: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    net_pnl: Decimal
    trade_count: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    profit_factor: Decimal | None
    max_drawdown: Decimal
    max_drawdown_pct: Decimal
    sharpe_ratio: Decimal | None
    funding_paid: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return convert_for_json(asdict(self))


@dataclass(frozen=True)
class BacktestResult:
    config: BacktestConfig
    candles_loaded: int
    candles_used: int
    metrics: BacktestMetrics
    final_account: PaperAccountSnapshot
    equity_curve: list[BacktestEquityPoint]
    trades: list[BacktestTrade]
    orders: list[PaperOrder]
    fills: list[PaperFill]
    reports: list[PaperExecutionReport] = field(default_factory=list)

    def to_dict(self, *, recent_count: int = 10) -> dict[str, Any]:
        recent_count = max(recent_count, 0)
        recent_slice = slice(-recent_count, None) if recent_count else slice(0, 0)
        accepted_reports = [report for report in self.reports if report.accepted]
        diagnostics = backtest_report_diagnostics(self.reports, self.trades)
        signal_funnel = backtest_signal_funnel(
            config=self.config,
            reports=self.reports,
            trades=self.trades,
            candles_loaded=self.candles_loaded,
            candles_used=self.candles_used,
        )
        return {
            "config": self.config.to_dict(),
            "candles_loaded": self.candles_loaded,
            "candles_used": self.candles_used,
            "metrics": self.metrics.to_dict(),
            "final_account": self.final_account.to_dict(),
            "trade_count": len(self.trades),
            "order_count": len(self.orders),
            "fill_count": len(self.fills),
            "accepted_report_count": len(accepted_reports),
            "rejected_report_count": len(self.reports) - len(accepted_reports),
            "diagnostics": convert_for_json(diagnostics),
            "signal_funnel": convert_for_json(signal_funnel),
            "recent_trades": [trade.to_dict() for trade in self.trades[recent_slice]],
            "recent_orders": [order.to_dict() for order in self.orders[recent_slice]],
            "recent_fills": [fill.to_dict() for fill in self.fills[recent_slice]],
            "equity_curve_tail": [
                point.to_dict() for point in self.equity_curve[recent_slice]
            ],
        }


def backtest_report_diagnostics(
    reports: list[PaperExecutionReport],
    trades: list[BacktestTrade] | None = None,
) -> dict[str, Any]:
    signal_action_counts: dict[str, int] = {}
    filled_action_counts: dict[str, int] = {}
    entry_rejection_reasons: dict[str, int] = {}
    exit_rejection_reasons: dict[str, int] = {}
    other_rejection_reasons: dict[str, int] = {}
    hold_count = 0
    ambiguous_exit_count = 0
    gap_exit_count = 0
    exit_reason_counts: dict[str, int] = {}
    exit_reason_total_net_pnl: dict[str, Decimal] = {}
    exit_reason_average_net_pnl: dict[str, Decimal] = {}
    atr_exit_count = 0
    stop_exit_count = 0
    take_profit_exit_count = 0
    mfe_values: list[Decimal] = []
    mae_values: list[Decimal] = []
    r_values: list[Decimal] = []
    net_pnls: list[Decimal] = []
    profile_distribution: dict[str, int] = {}
    setup_tier_distribution: dict[str, int] = {}
    setup_tier_net_pnl: dict[str, Decimal] = {}
    setup_tier_wins: dict[str, int] = {}
    direction_distribution: dict[str, int] = {}
    direction_net_pnl: dict[str, Decimal] = {}
    direction_wins: dict[str, int] = {}

    for report in reports:
        signal = report.signal
        if signal is None and report.risk_decision is not None:
            signal = report.risk_decision.signal
        action = signal.action.value if signal is not None else "unknown"
        signal_action_counts[action] = signal_action_counts.get(action, 0) + 1

        if report.accepted:
            metadata = signal.metadata if signal is not None else {}
            if metadata.get("ambiguous_candle"):
                ambiguous_exit_count += 1
            if metadata.get("gap_exit"):
                gap_exit_count += 1
            if report.order is not None:
                filled_action = report.order.action.value
                filled_action_counts[filled_action] = (
                    filled_action_counts.get(filled_action, 0) + 1
                )
            continue

        reason = (
            report.risk_decision.reason
            if report.risk_decision is not None
            else report.reason
        )
        if action == "hold":
            hold_count += 1
        elif action in {"enter_long", "enter_short"}:
            _increment(entry_rejection_reasons, reason)
        elif action in {"exit_long", "exit_short"}:
            _increment(exit_rejection_reasons, reason)
        else:
            _increment(other_rejection_reasons, reason)

    for trade in trades or []:
        _increment(exit_reason_counts, trade.exit_reason)
        exit_reason_total_net_pnl[trade.exit_reason] = (
            exit_reason_total_net_pnl.get(trade.exit_reason, Decimal("0"))
            + trade.net_pnl
        )
        net_pnls.append(trade.net_pnl)
        atr_profile = str(trade.metadata.get("atr_profile") or "none")
        setup_tier = str(trade.metadata.get("setup_tier") or "none")
        direction = trade.direction.value
        _increment(profile_distribution, atr_profile)
        _increment(setup_tier_distribution, setup_tier)
        _increment(direction_distribution, direction)
        setup_tier_net_pnl[setup_tier] = (
            setup_tier_net_pnl.get(setup_tier, Decimal("0")) + trade.net_pnl
        )
        direction_net_pnl[direction] = (
            direction_net_pnl.get(direction, Decimal("0")) + trade.net_pnl
        )
        if trade.won:
            setup_tier_wins[setup_tier] = setup_tier_wins.get(setup_tier, 0) + 1
            direction_wins[direction] = direction_wins.get(direction, 0) + 1
        normalized_reason = trade.exit_reason.lower()
        if "atr" in normalized_reason:
            atr_exit_count += 1
        if "stop" in normalized_reason:
            stop_exit_count += 1
        if "take profit" in normalized_reason or "target" in normalized_reason:
            take_profit_exit_count += 1

        mfe = _decimal_metadata(trade.metadata.get("mfe"))
        mae = _decimal_metadata(trade.metadata.get("mae"))
        r_multiple = _decimal_metadata(trade.metadata.get("r_multiple"))
        if mfe is not None:
            mfe_values.append(mfe)
        if mae is not None:
            mae_values.append(mae)
        if r_multiple is not None:
            r_values.append(r_multiple)

    for reason, count in exit_reason_counts.items():
        if count:
            exit_reason_average_net_pnl[reason] = (
                exit_reason_total_net_pnl[reason] / Decimal(count)
            )

    return {
        "signal_action_counts": signal_action_counts,
        "filled_action_counts": filled_action_counts,
        "hold_count": hold_count,
        "entry_rejection_reasons": entry_rejection_reasons,
        "exit_rejection_reasons": exit_rejection_reasons,
        "other_rejection_reasons": other_rejection_reasons,
        "ambiguous_exit_count": ambiguous_exit_count,
        "gap_exit_count": gap_exit_count,
        "exit_reason_counts": exit_reason_counts,
        "exit_reason_total_net_pnl": exit_reason_total_net_pnl,
        "exit_reason_average_net_pnl": exit_reason_average_net_pnl,
        "atr_exit_count": atr_exit_count,
        "stop_exit_count": stop_exit_count,
        "take_profit_exit_count": take_profit_exit_count,
        "average_mfe": _average(mfe_values),
        "average_mae": _average(mae_values),
        "average_r_multiple": _average(r_values),
        "largest_win": max(net_pnls) if net_pnls else None,
        "largest_loss": min(net_pnls) if net_pnls else None,
        "profile_distribution": profile_distribution,
        "setup_tier_distribution": setup_tier_distribution,
        "setup_tier_performance": _group_performance(
            setup_tier_distribution,
            setup_tier_net_pnl,
            setup_tier_wins,
        ),
        "direction_distribution": direction_distribution,
        "direction_performance": _group_performance(
            direction_distribution,
            direction_net_pnl,
            direction_wins,
        ),
    }


def backtest_signal_funnel(
    *,
    config: BacktestConfig,
    reports: list[PaperExecutionReport],
    trades: list[BacktestTrade],
    candles_loaded: int,
    candles_used: int,
) -> dict[str, Any]:
    counts: dict[str, int] = {
        "requested_candles": config.requested_candles or candles_loaded,
        "actual_candles_loaded": candles_loaded,
        "candles_evaluated": candles_used,
        "raw_long_candidates": 0,
        "raw_short_candidates": 0,
        "blocked_by_component_disagreement": 0,
        "blocked_by_visual_screen": 0,
        "blocked_by_atr_low_volatility": 0,
        "blocked_by_atr_high_volatility": 0,
        "blocked_by_cooldown": 0,
        "blocked_by_existing_open_position": 0,
        "blocked_by_daily_loss_guard": 0,
        "blocked_by_max_exposure_margin": 0,
        "blocked_by_oi_short_restriction": 0,
        "final_executed_entries": 0,
        "closed_trades": len(trades),
    }

    for report in reports:
        signal = _report_signal(report)
        action = signal.action.value if signal is not None else ""
        metadata = signal.metadata if signal is not None else {}
        reason = _report_reason(report).lower()

        raw_direction = metadata.get("signal_funnel_raw_direction")
        if raw_direction == "long":
            counts["raw_long_candidates"] += 1
        elif raw_direction == "short":
            counts["raw_short_candidates"] += 1
        elif action == "enter_long":
            counts["raw_long_candidates"] += 1
        elif action == "enter_short":
            counts["raw_short_candidates"] += 1

        if report.accepted and report.order is not None:
            filled_action = report.order.action.value
            if filled_action in {"enter_long", "enter_short"}:
                counts["final_executed_entries"] += 1
            continue

        if "components do not agree" in reason or "component" in reason:
            counts["blocked_by_component_disagreement"] += 1
        if "visual" in reason or _metadata_visual_blocked(metadata):
            counts["blocked_by_visual_screen"] += 1
        if "low volatility" in reason or "natr is too low" in reason:
            counts["blocked_by_atr_low_volatility"] += 1
        if "high volatility" in reason or "volatility is overheated" in reason:
            counts["blocked_by_atr_high_volatility"] += 1
        if "cooldown" in reason:
            counts["blocked_by_cooldown"] += 1
        if "already open" in reason or "max open positions" in reason:
            counts["blocked_by_existing_open_position"] += 1
        if "max daily loss" in reason or "kill switch" in reason:
            counts["blocked_by_daily_loss_guard"] += 1
        if (
            "open notional" in reason
            or "total exposure" in reason
            or "total risk" in reason
            or "margin" in reason
            or "planned risk" in reason
            or "position notional" in reason
        ):
            counts["blocked_by_max_exposure_margin"] += 1
        if "open-interest" in reason or "open interest" in reason or "short setup" in reason:
            counts["blocked_by_oi_short_restriction"] += 1

    return counts


def _group_performance(
    counts: dict[str, int],
    net_pnl: dict[str, Decimal],
    wins: dict[str, int],
) -> dict[str, dict[str, Decimal | int | None]]:
    performance: dict[str, dict[str, Decimal | int | None]] = {}
    for key, count in counts.items():
        total = net_pnl.get(key, Decimal("0"))
        performance[key] = {
            "count": count,
            "net_pnl": total,
            "average_net_pnl": total / Decimal(count) if count else None,
            "win_rate_pct": (
                Decimal(wins.get(key, 0)) / Decimal(count) * Decimal("100")
                if count
                else None
            ),
        }
    return performance


def _report_signal(report: PaperExecutionReport):
    if report.signal is not None:
        return report.signal
    if report.risk_decision is not None:
        return report.risk_decision.signal
    return None


def _report_reason(report: PaperExecutionReport) -> str:
    if report.risk_decision is not None and report.risk_decision.reason:
        return report.risk_decision.reason
    return report.reason or ""


def _metadata_visual_blocked(metadata: dict[str, Any]) -> bool:
    visual = metadata.get("visual")
    return isinstance(visual, dict) and bool(visual.get("blocked"))


def _increment(values: dict[str, int], key: str) -> None:
    values[key] = values.get(key, 0) + 1


def _decimal_metadata(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _average(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))
