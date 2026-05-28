from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.broker.models import (
    PaperAccountSnapshot,
    PaperExecutionReport,
    PaperFill,
    PaperOrder,
)
from app.risk.models import convert_for_json
from app.strategies.base import SignalAction, SignalDirection, SignalFunnelReason


@dataclass(frozen=True)
class BacktestConfig:
    pair: str
    interval: str
    starting_equity: Decimal
    leverage: Decimal
    strategy_name: str = "all"
    margin_currency: str = "INR"
    price_quote_currency: str = "USDT"
    quote_to_margin_rate: Decimal = Decimal("1")
    unit_contract_value: Decimal = Decimal("1")
    requested_candles: int | None = None
    risk_per_trade_pct: Decimal | None = None
    compound_risk_equity: bool = True
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
    atr_trailing_multiple: Decimal = Decimal("1.2")
    atr_take_profit_mode: str = "none"
    bb_trail_enabled: bool = False
    bb_trail_buffer_multiplier: Decimal = Decimal("1.0")
    bb_trail_activation_r: Decimal = Decimal("0.5")
    bb_trail_stage2_r: Decimal = Decimal("0.5")
    bb_trail_stage3_r: Decimal = Decimal("1.0")
    bb_trail_force_close_r: Decimal = Decimal("4.0")
    bb_trail_partial_close_at_tp: bool = True
    bb_trail_partial_close_pct: Decimal = Decimal("0.60")
    execution_interval: str | None = None
    paper_intrabar_enabled: bool = False
    strategy_interval: str | None = None
    use_partial_parent_candle: bool = False
    max_entries_per_parent_candle: int = 1
    enter_on_execution_close: bool = True
    intrabar_reentry_enabled: bool = False
    max_reentries_per_candle: int = 0
    reentry_cooldown_candles: int = 1
    previous_parent_high: Decimal | None = None
    previous_parent_low: Decimal | None = None
    intrabar_reversal_breakout_enabled: bool = True
    reversal_breakout_min_execution_candles: int = 2
    reversal_breakout_volume_ratio: Decimal = Decimal("2.0")
    reversal_breakout_body_ratio: Decimal = Decimal("0.65")
    reversal_breakout_close_position_ratio: Decimal = Decimal("0.70")
    reversal_breakout_risk_multiplier: Decimal = Decimal("0.50")
    reversal_breakout_max_extension_atr: Decimal = Decimal("2.2")
    reversal_breakout_ignition_volume_ratio: Decimal = Decimal("3.0")
    reversal_breakout_ignition_body_ratio: Decimal = Decimal("0.70")
    reversal_breakout_ignition_close_position_ratio: Decimal = Decimal("0.75")
    reversal_breakout_ignition_max_extension_atr: Decimal = Decimal("5.0")
    reversal_breakout_ignition_risk_multiplier: Decimal = Decimal("0.25")
    reversal_breakout_breakeven_activation_r: Decimal = Decimal("0.70")
    reversal_breakout_profit_lock_activation_r: Decimal = Decimal("1.20")
    reversal_breakout_profit_lock_r: Decimal = Decimal("0.35")
    reversal_breakout_time_stop_candles: int = 8
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

    # Entry Timing and Quality
    balanced_breakout_enabled: bool = True
    balanced_breakout_volume_ratio_min: Decimal = Decimal("1.8")
    balanced_breakout_body_ratio_min: Decimal = Decimal("0.60")
    balanced_breakout_close_position_min: Decimal = Decimal("0.70")
    balanced_breakout_max_extension_atr: Decimal = Decimal("2.0")
    balanced_breakout_max_age_candles: int = 2
    balanced_breakout_risk_multiplier: Decimal = Decimal("0.50")
    
    false_breakout_filter_enabled: bool = True
    false_breakout_max_wick_ratio: Decimal = Decimal("0.45")
    false_breakout_require_close_outside_parent: bool = True
    
    late_chase_block_enabled: bool = True
    late_chase_max_consecutive_impulse_candles: int = 3
    late_chase_volume_fade_ratio: Decimal = Decimal("0.75")
    late_chase_max_extension_atr: Decimal = Decimal("2.2")
    
    pullback_entry_enabled: bool = True
    pullback_max_age_candles: int = 8
    pullback_max_distance_from_ema_atr: Decimal = Decimal("0.6")
    pullback_resume_body_ratio_min: Decimal = Decimal("0.45")
    pullback_risk_multiplier: Decimal = Decimal("0.50")
    
    signal_flip_grace_candles: int = 2
    signal_flip_confirm_candles: int = 2
    time_stop_extend_if_momentum_strong: bool = True
    
    # Profit Locking (Virtual Accounting)
    profit_locking_enabled: bool = True
    auto_lock_profit_pct: Decimal = Decimal("100")

    def __post_init__(self) -> None:
        if self.requested_candles is not None and self.requested_candles <= 0:
            raise ValueError("requested_candles must be positive when provided.")
        if self.quote_to_margin_rate <= 0:
            raise ValueError("quote_to_margin_rate must be positive.")
        if self.unit_contract_value <= 0:
            raise ValueError("unit_contract_value must be positive.")
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
        if self.atr_trailing_multiple <= 0:
            raise ValueError("atr_trailing_multiple must be positive.")
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
        if self.max_daily_loss_pct <= 0:
            raise ValueError("max_daily_loss_pct must be positive.")
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
    
    # New ROE and Notional fields
    entry_notional: Decimal = Decimal("0")
    exit_notional: Decimal = Decimal("0")
    leverage: Decimal = Decimal("1")
    margin_used: Decimal = Decimal("0")
    net_pct_of_notional: Decimal = Decimal("0")
    gross_roe_pct: Decimal = Decimal("0")
    net_roe_pct: Decimal = Decimal("0")
    account_equity_at_entry: Decimal = Decimal("0")
    account_impact_pct: Decimal = Decimal("0")
    
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
    
    # Profit Locking Metrics
    final_total_equity: Decimal = Decimal("0")
    final_tradable_equity: Decimal = Decimal("0")
    final_locked_profit: Decimal = Decimal("0")
    max_daily_loss_hit_count: int = 0
    trades_blocked_by_profit_lock_or_daily_loss: int = 0
    returns_total_equity_pct: Decimal = Decimal("0")
    returns_tradable_equity_pct: Decimal = Decimal("0")

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
        diagnostics = backtest_report_diagnostics(self.reports, self.trades, self.equity_curve)
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
    equity_curve: list[BacktestEquityPoint] | None = None,
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

    all_trades = trades or []
    for trade in all_trades:
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

    # Advanced Fragility Metrics
    long_trades = [t for t in all_trades if t.direction == SignalDirection.LONG]
    short_trades = [t for t in all_trades if t.direction == SignalDirection.SHORT]
    
    total_net_pnl = sum(net_pnls, Decimal("0"))
    winning_trades = [t for t in all_trades if t.won]
    top_5_winners = sorted(winning_trades, key=lambda t: t.net_pnl, reverse=True)[:5]
    top_5_winners_pnl = sum((t.net_pnl for t in top_5_winners), Decimal("0"))
    
    dependency = {
        "largest_win": max(net_pnls) if net_pnls else None,
        "largest_loss": min(net_pnls) if net_pnls else None,
        "top_5_winners_total_pnl": top_5_winners_pnl,
        "top_5_dependency_pct": (
            (top_5_winners_pnl / total_net_pnl * 100)
            if total_net_pnl > 0
            else Decimal("0")
        ),
        "net_pnl_excluding_top_5": total_net_pnl - top_5_winners_pnl,
    }

    direction_perf = {
        "long": _perf_summary(long_trades),
        "short": _perf_summary(short_trades),
    }

    # PnL Split by Thirds
    thirds_pnl = {"first": Decimal("0"), "middle": Decimal("0"), "final": Decimal("0")}
    if all_trades:
        first_third_idx = len(all_trades) // 3
        second_third_idx = (len(all_trades) * 2) // 3
        thirds_pnl["first"] = sum((t.net_pnl for t in all_trades[:first_third_idx]), Decimal("0"))
        thirds_pnl["middle"] = sum((t.net_pnl for t in all_trades[first_third_idx:second_third_idx]), Decimal("0"))
        thirds_pnl["final"] = sum((t.net_pnl for t in all_trades[second_third_idx:]), Decimal("0"))

    # Best/Worst Day (based on equity curve)
    daily_stats = {}
    if equity_curve:
        for point in equity_curve:
            # Simple day grouping: YYYY-MM-DD
            day = datetime.fromtimestamp(point.timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            if day not in daily_stats:
                daily_stats[day] = {"start": point.realized_pnl, "end": point.realized_pnl}
            daily_stats[day]["end"] = point.realized_pnl
            
    best_day = None
    worst_day = None
    if daily_stats:
        day_pnls = {day: stats["end"] - stats["start"] for day, stats in daily_stats.items()}
        best_day_str = max(day_pnls, key=lambda d: day_pnls[d])
        worst_day_str = min(day_pnls, key=lambda d: day_pnls[d])
        best_day = {"date": best_day_str, "pnl": day_pnls[best_day_str]}
        worst_day = {"date": worst_day_str, "pnl": day_pnls[worst_day_str]}

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
        "largest_win": dependency["largest_win"],
        "largest_loss": dependency["largest_loss"],
        "dependency": dependency,
        "direction_performance_v2": direction_perf,
        "thirds_pnl": thirds_pnl,
        "best_day": best_day,
        "worst_day": worst_day,
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


def _perf_summary(trades: list[BacktestTrade]) -> dict[str, Any]:
    if not trades:
        return {
            "count": 0,
            "net_pnl": Decimal("0"),
            "win_rate_pct": Decimal("0"),
            "profit_factor": Decimal("0"),
        }
    
    net_pnl = sum((t.net_pnl for t in trades), Decimal("0"))
    wins = [t for t in trades if t.won]
    losses = [t for t in trades if not t.won]
    
    total_wins = sum((t.net_pnl for t in wins), Decimal("0"))
    total_losses = abs(sum((t.net_pnl for t in losses), Decimal("0")))
    
    return {
        "count": len(trades),
        "net_pnl": net_pnl,
        "win_rate_pct": Decimal(len(wins)) / Decimal(len(trades)) * 100,
        "profit_factor": (total_wins / total_losses) if total_losses > 0 else (Decimal("100") if total_wins > 0 else Decimal("0")),
    }


def backtest_signal_funnel(
    *,
    config: BacktestConfig,
    reports: list[PaperExecutionReport],
    trades: list[BacktestTrade],
    candles_loaded: int,
    candles_used: int,
) -> dict[str, Any]:
    buckets = {reason.value: 0 for reason in SignalFunnelReason}
    
    # Initialize funnel accounting
    counts: dict[str, int] = {
        "requested_candles": config.requested_candles or candles_loaded,
        "actual_candles_loaded": candles_loaded,
        "candles_evaluated": candles_used,
        "raw_long_candidates": 0,
        "raw_short_candidates": 0,
        "raw_candidates_total": 0,
        "executed_entries": 0,
        "explained_rejections_total": 0,
        "unexplained_candidates_total": 0,
        "accounted_candidates_total": 0,
        "closed_trades": len(trades),
    }

    processed_reports = 0

    for report in reports:
        signal = _report_signal(report)
        if signal is None:
            continue
            
        metadata = signal.metadata or {}
        
        # Only account for reports that were originally raw entry candidates
        is_raw = metadata.get("signal_funnel_raw_candidate", False)
        # If metadata is missing, we infer from action
        if not is_raw and signal.action in {SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT}:
            is_raw = True
            
        if not is_raw:
            continue
            
        processed_reports += 1
        
        raw_direction = metadata.get("signal_funnel_raw_direction")
        if raw_direction == "long":
            counts["raw_long_candidates"] += 1
        elif raw_direction == "short":
            counts["raw_short_candidates"] += 1
        elif signal.direction == SignalDirection.LONG:
            counts["raw_long_candidates"] += 1
        elif signal.direction == SignalDirection.SHORT:
            counts["raw_short_candidates"] += 1
            
        # 1. Executed
        if report.accepted and report.order is not None:
            filled_action = report.order.action
            if filled_action in {SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT}:
                counts["executed_entries"] += 1
                buckets[SignalFunnelReason.EXECUTED.value] += 1
                continue

        # 2. Blocked by Strategy Reason Code
        funnel_reason = signal.funnel_reason
        if funnel_reason:
            buckets[funnel_reason.value] += 1
            counts["explained_rejections_total"] += 1
            continue
            
        # 3. Fallback to Textual Parsing for Risk/Execution
        reason = _report_reason(report).lower()
        mapped_reason = None
        
        if "cooldown" in reason:
            mapped_reason = SignalFunnelReason.COOLDOWN_BLOCKED
        elif any(x in reason for x in ["already long", "already short", "already open", "max open positions", "position open"]):
            mapped_reason = SignalFunnelReason.EXISTING_POSITION_BLOCKED
        elif any(x in reason for x in ["max daily loss", "kill switch", "equity is depleted", "equity must be positive"]):
            mapped_reason = SignalFunnelReason.DAILY_LOSS_GUARD_BLOCKED
        elif any(x in reason for x in ["open notional", "total exposure", "total risk", "margin", "planned risk", "position notional", "leverage exceeds"]):
            mapped_reason = SignalFunnelReason.EXPOSURE_MARGIN_BLOCKED
        elif any(x in reason for x in ["open-interest", "open interest", "short setup", "short restriction"]):
            mapped_reason = SignalFunnelReason.OI_SHORT_RESTRICTION
        elif "atr policy" in reason:
            mapped_reason = SignalFunnelReason.ATR_POLICY_ENTRY_BLOCKED
        elif any(x in reason for x in ["components do not agree", "secondary strategy disagrees", "agreement below"]):
            mapped_reason = SignalFunnelReason.AGREEMENT_BELOW_MINIMUM
        elif "visual" in reason:
            mapped_reason = SignalFunnelReason.VISUAL_SCREEN_BLOCKED
        elif "threshold" in reason:
            mapped_reason = SignalFunnelReason.BELOW_ENTRY_THRESHOLD

        if mapped_reason:
            buckets[mapped_reason.value] += 1
            counts["explained_rejections_total"] += 1
        else:
            # Check for strategy-level hold that wasn't mapped
            if signal.action == SignalAction.HOLD:
                buckets[SignalFunnelReason.STRATEGY_HOLD_UNMAPPED.value] += 1
                counts["explained_rejections_total"] += 1
            else:
                counts["unexplained_candidates_total"] += 1
                buckets[SignalFunnelReason.UNEXPLAINED_CANDIDATE.value] += 1

    counts["raw_candidates_total"] = counts["raw_long_candidates"] + counts["raw_short_candidates"]
    counts["accounted_candidates_total"] = counts["executed_entries"] + counts["explained_rejections_total"] + counts["unexplained_candidates_total"]
    
    # Sanity check: If total raw > processed, the discrepancy is unexplained
    if counts["raw_candidates_total"] > processed_reports:
        diff = counts["raw_candidates_total"] - processed_reports
        counts["unexplained_candidates_total"] += diff
        counts["accounted_candidates_total"] += diff
        buckets[SignalFunnelReason.UNEXPLAINED_CANDIDATE.value] += diff

    return {**counts, "block_reasons": buckets}


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
