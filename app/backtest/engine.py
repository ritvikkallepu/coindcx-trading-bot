from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from app.backtest.metrics import compute_backtest_metrics
from app.backtest.models import (
    BacktestConfig,
    BacktestEquityPoint,
    BacktestResult,
    BacktestTrade,
    _decimal_metadata,
)
from app.broker.models import PaperExecutionReport, PaperFill, PaperOrder
from app.broker.paper import PaperBroker
from app.data.candle_builder import CandleSeries, OHLCVCandle, interval_to_ms
from app.data.indicators import latest_indicator_snapshot
from app.data.open_interest import OpenInterestFeatureSeries
from app.execution.engine import PaperExecutionEngine
from app.risk.limits import daily_loss_limit_amount
from app.risk.manager import RiskManager
from app.risk.pair_profiles import apply_pair_profile_to_config
from app.risk.models import InstrumentMetadata, OpenPosition, RiskDecision
from app.strategies.base import (
    SignalAction,
    SignalDirection,
    SignalFunnelReason,
    StrategyContext,
    StrategyEngine,
    StrategySignal,
)


ENTRY_SAFETY_SPIKE_ATR_MULTIPLE = Decimal("3.5")
ENTRY_SAFETY_OPPOSITE_BODY_ATR_MULTIPLE = Decimal("0.75")
ENTRY_SAFETY_EXHAUSTION_LOOKBACK = 4
ENTRY_SAFETY_EXHAUSTION_ATR_MULTIPLE = Decimal("3")
ENTRY_SAFETY_COMPONENT_MIN_SCORE = Decimal("0.10")
ENTRY_SAFETY_COMPONENT_STRONG_OPPOSITE = Decimal("0.55")
ENTRY_SAFETY_HIGHER_CONTEXT_GROUP = 4


@dataclass
class _OpenTradeLeg:
    entry_fee: Decimal


@dataclass
class _PendingDecision:
    decision: RiskDecision
    generated_at_ms: int


@dataclass(frozen=True)
class _ATRExecutionSettings:
    stop_enabled: bool
    take_profit_enabled: bool
    trailing_enabled: bool
    stop_multiple: Decimal
    take_profit_multiple: Decimal
    trailing_multiple: Decimal
    take_profit_mode: str
    policy_mode: str
    risk_multiplier: Decimal


@dataclass
class _BacktestSafetyState:
    config: BacktestConfig
    interval_ms: int
    peak_equity: Decimal
    consecutive_losses: int = 0
    recent_trades: list[BacktestTrade] = field(default_factory=list)
    equity_history: list[tuple[int, Decimal]] = field(default_factory=list)
    
    cooldown_until_ms: int = 0
    stop_cooldown_until_ms: int = 0
    giveback_cooldown_until_ms: int = 0
    post_spike_cooldown_until_ms: int = 0
    
    blocked_count_streak: int = 0
    blocked_count_rolling: int = 0
    blocked_count_giveback: int = 0
    blocked_count_post_spike: int = 0

    def active(self, timestamp_ms: int) -> bool:
        return timestamp_ms < self.max_cooldown(timestamp_ms)

    def max_cooldown(self, timestamp_ms: int) -> int:
        return max(
            self.cooldown_until_ms,
            self.stop_cooldown_until_ms,
            self.giveback_cooldown_until_ms,
            self.post_spike_cooldown_until_ms
        )

    def reason(self, timestamp_ms: int) -> str:
        if timestamp_ms < self.stop_cooldown_until_ms:
            remaining_ms = self.stop_cooldown_until_ms - timestamp_ms
            return (
                "Stop-loss cooldown active after a losing stop exit; "
                f"remaining_ms={remaining_ms}."
            )
        if timestamp_ms < self.giveback_cooldown_until_ms:
            return "equity_giveback_guard"
        if timestamp_ms < self.post_spike_cooldown_until_ms:
            return "post_spike_cooldown"
        if timestamp_ms < self.cooldown_until_ms:
            remaining_ms = max(self.cooldown_until_ms - timestamp_ms, 0)
            return (
                "Loss-streak cooldown active after "
                f"{self.consecutive_losses} consecutive losing trade(s); "
                f"remaining_ms={remaining_ms}."
            )
        return "none"

    def observe_candle(self, candle: OHLCVCandle, equity: Decimal) -> None:
        self.equity_history.append((candle.close_time_ms, equity))
        if len(self.equity_history) > max(100, self.config.post_spike_lookback_candles + 1):
            self.equity_history.pop(0)

        # Task 2: Equity giveback
        if self.config.equity_giveback_guard_enabled:
            self.peak_equity = max(self.peak_equity, equity)
            if self.peak_equity > 0:
                giveback = (self.peak_equity - equity) / self.peak_equity
                if giveback >= self.config.equity_giveback_threshold_pct:
                    self.giveback_cooldown_until_ms = max(
                        self.giveback_cooldown_until_ms,
                        candle.close_time_ms + self.config.equity_giveback_cooldown_candles * self.interval_ms
                    )

        # Task 4: Post-spike cooldown
        if self.config.post_spike_cooldown_enabled:
            lookback = self.config.post_spike_lookback_candles
            if len(self.equity_history) > lookback:
                past_equity = self.equity_history[-lookback][1]
                if past_equity > 0:
                    gain = (equity - past_equity) / past_equity
                    if gain >= self.config.post_spike_gain_threshold_pct:
                        self.post_spike_cooldown_until_ms = max(
                            self.post_spike_cooldown_until_ms,
                            candle.close_time_ms + self.config.post_spike_cooldown_candles * self.interval_ms
                        )

    def observe_trade(self, trade: BacktestTrade) -> None:
        self.recent_trades.append(trade)
        if len(self.recent_trades) > 50:
            self.recent_trades.pop(0)

        if trade.net_pnl < 0:
            # Existing stop cooldown logic
            stop_cooldown_ms = self.config.stop_loss_cooldown_candles * self.interval_ms
            if stop_cooldown_ms > 0 and _is_stop_exit_reason(trade.exit_reason):
                self.stop_cooldown_until_ms = max(
                    self.stop_cooldown_until_ms,
                    trade.exit_time_ms + stop_cooldown_ms,
                )

            # Task 3: Loss-streak cooldown
            self.consecutive_losses += 1
            
            # Consecutive limit
            consecutive_limit = self.config.consecutive_loss_limit if self.config.loss_streak_cooldown_enabled else self.config.max_consecutive_losses
            streak_cooldown_ms = (self.config.loss_streak_cooldown_candles if self.config.loss_streak_cooldown_enabled else self.config.loss_cooldown_candles) * self.interval_ms
            
            if consecutive_limit > 0 and self.consecutive_losses >= consecutive_limit:
                self.cooldown_until_ms = max(
                    self.cooldown_until_ms,
                    trade.exit_time_ms + streak_cooldown_ms,
                )

            # Rolling window
            if self.config.loss_streak_cooldown_enabled:
                window = self.config.rolling_loss_window
                rolling_limit = self.config.rolling_loss_limit
                if len(self.recent_trades) >= window:
                    window_trades = self.recent_trades[-window:]
                    losses_in_window = sum(1 for t in window_trades if t.net_pnl < 0)
                    if losses_in_window >= rolling_limit:
                        self.cooldown_until_ms = max(
                            self.cooldown_until_ms,
                            trade.exit_time_ms + self.config.rolling_loss_cooldown_candles * self.interval_ms,
                        )
        else:
            self.consecutive_losses = 0
            if trade.exit_time_ms >= self.cooldown_until_ms:
                self.cooldown_until_ms = 0

    def record_blocked(self, reason: str) -> None:
        if "equity_giveback_guard" in reason:
            self.blocked_count_giveback += 1
        elif "post_spike_cooldown" in reason:
            self.blocked_count_post_spike += 1
        elif "loss_streak_cooldown" in reason:
            # We don't distinguish between rolling and consecutive for count currently
            # but we could if we want. Let's just count it as streak for now.
            self.blocked_count_streak += 1


class BacktestEngine:
    def __init__(
        self,
        *,
        config: BacktestConfig,
        strategy_engine: StrategyEngine,
        risk_manager: RiskManager,
        instrument: InstrumentMetadata | None = None,
        open_interest_features: OpenInterestFeatureSeries | None = None,
    ) -> None:
        self.config = config
        self.strategy_engine = strategy_engine
        self.risk_manager = risk_manager
        self.instrument = instrument
        self.open_interest_features = open_interest_features or OpenInterestFeatureSeries([])

    def run(
        self,
        candles: Iterable[OHLCVCandle],
        *,
        execution_candles: Iterable[OHLCVCandle] | None = None,
    ) -> BacktestResult:
        all_candles = sorted(candles, key=lambda candle: candle.open_time_ms)
        if not all_candles:
            raise ValueError("Backtest requires at least one candle.")
        if execution_candles is not None:
            return self._run_intrabar(all_candles, execution_candles)

        broker = PaperBroker(
            starting_equity=self.config.starting_equity,
            maker_fee_rate=self.config.maker_fee_rate,
            taker_fee_rate=self.config.taker_fee_rate,
            fee_gst_rate=self.config.fee_gst_rate,
            entry_fee_type=self.config.entry_fee_type,
            exit_fee_type=self.config.exit_fee_type,
            slippage_pct=self.config.slippage_pct,
            stop_slippage_pct=self.config.stop_slippage_pct,
            trailing_stop_enabled=self.config.trailing_stop_enabled,
            trailing_stop_activation_pct=self.config.trailing_stop_activation_pct,
            trailing_stop_distance_pct=self.config.trailing_stop_distance_pct,
            quote_to_margin_rate=self.config.quote_to_margin_rate,
            unit_contract_value=self.config.unit_contract_value,
            account_currency=self.config.margin_currency,
            price_quote_currency=self.config.price_quote_currency,
        )
        broker.profit_lock_enabled = self.config.profit_locking_enabled
        broker.auto_lock_profit_pct = self.config.auto_lock_profit_pct

        execution = PaperExecutionEngine(broker)
        series = CandleSeries(maxlen=max(len(all_candles), 1))
        reports: list[PaperExecutionReport] = []
        trades: list[BacktestTrade] = []
        equity_curve: list[BacktestEquityPoint] = []
        open_trade_legs: dict[str, _OpenTradeLeg] = {}
        pending_decisions: list[_PendingDecision] = []
        daily_net_pnl: dict[str, Decimal] = {}
        daily_start_equity: dict[str, Decimal] = {}
        halted_days: set[str] = set()
        
        max_daily_loss_hit_count = 0
        trades_blocked_by_profit_lock_or_daily_loss = 0
        
        safety_state = _BacktestSafetyState(
            config=self.config,
            interval_ms=interval_to_ms(self.config.interval),
            peak_equity=self.config.starting_equity,
        )

        for candle in all_candles:
            day = _day_key(candle.close_time_ms)
            if day not in daily_start_equity:
                daily_start_equity[day] = broker.snapshot({self.config.pair: candle.open}).tradable_base

            if pending_decisions:
                # ... rest of loop ...
                pending_decisions = _fill_pending_decisions(
                    pending_decisions,
                    execution=execution,
                    candle=candle,
                    reports=reports,
                    trades=trades,
                    open_trade_legs=open_trade_legs,
                    daily_net_pnl=daily_net_pnl,
                    safety_state=safety_state,
                )

            for report in execution.process_candle(candle):
                reports.append(report)
                _record_report(
                    report,
                    trades=trades,
                    open_trade_legs=open_trade_legs,
                    daily_net_pnl=daily_net_pnl,
                    safety_state=safety_state,
                )

            funding_paid = _apply_funding_if_due(
                broker=broker,
                config=self.config,
                candle=candle,
            )
            if funding_paid != 0:
                daily_net_pnl[day] = daily_net_pnl.get(day, Decimal("0")) - funding_paid

            series.add(candle)
            
            # Update safety state with latest equity
            current_snapshot = broker.snapshot({self.config.pair: candle.close})
            safety_state.observe_candle(candle, current_snapshot.equity)

            if _daily_loss_kill_switch_reached(
                broker=broker,
                candle=candle,
                day_start_equity=daily_start_equity[day],
                risk_manager=self.risk_manager,
            ):
                max_daily_loss_hit_count += 1
                reports.extend(
                    _force_close_open_positions(
                        execution=execution,
                        broker=broker,
                        candle=candle,
                        reason="Max daily loss kill switch triggered.",
                        trades=trades,
                        open_trade_legs=open_trade_legs,
                        daily_net_pnl=daily_net_pnl,
                        safety_state=safety_state,
                    )
                )
                halted_days.add(day)

            if day in halted_days:
                equity_curve.append(_equity_point(broker, candle))
                continue

            indicators = latest_indicator_snapshot(
                series,
                atr_period=self.config.atr_period,
            )
            context = StrategyContext(
                pair=self.config.pair,
                interval=self.config.interval,
                candles=series,
                indicators=indicators,
                features=_strategy_features(
                    broker,
                    self.config.pair,
                    self.config,
                    open_interest=self.open_interest_features.latest_at_or_before(
                        candle.close_time_ms
                    ),
                ),
            )

            for signal in self.strategy_engine.evaluate(context):
                # Ensure every signal from evaluate is marked as a raw candidate if it has directional intent
                if signal.direction is not None and signal.action in {SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT, SignalAction.HOLD}:
                    if not signal.metadata.get("signal_funnel_raw_candidate"):
                        signal = replace(
                            signal,
                            metadata={
                                **signal.metadata,
                                "signal_funnel_raw_candidate": True,
                                "signal_funnel_raw_direction": signal.direction.value
                            }
                        )

                signal = _apply_exit_overrides(
                    signal,
                    self.config,
                    current_atr=indicators.atr,
                )
                snapshot = broker.snapshot({self.config.pair: candle.close})
                risk_equity = _risk_sizing_equity(
                    snapshot=snapshot,
                    config=self.config,
                )
                decision = self.risk_manager.evaluate_signal(
                    signal,
                    account_equity=risk_equity,
                    available_equity=snapshot.tradable_equity if self.config.profit_locking_enabled else snapshot.equity,
                    risk_base_mode=_risk_base_mode(self.config),
                    open_positions=_risk_open_positions(broker),
                    daily_realized_pnl=-snapshot.daily_loss_from_tradable_base if self.config.profit_locking_enabled else daily_net_pnl.get(_day_key(candle.close_time_ms), Decimal("0")),
                    daily_loss_limit_equity=snapshot.tradable_base if self.config.profit_locking_enabled else daily_start_equity[day],
                    instrument=self.instrument,
                    requested_leverage=self.config.leverage,
                    quote_to_margin_rate=self.config.quote_to_margin_rate,
                    unit_contract_value=self.config.unit_contract_value,
                    trading_mode="paper",
                    live_trading_enabled=False,
                    protected_profit_override_enabled=snapshot.protected_profit_override_enabled,
                )
                if not decision.approved and ("Daily loss reached" in decision.reason or "profit lock" in decision.reason.lower()):
                    trades_blocked_by_profit_lock_or_daily_loss += 1
                decision = _apply_entry_safety_filter(
                    decision,
                    series=series,
                    latest_candle=candle,
                    indicators=indicators,
                    config=self.config,
                )
                if _entry_blocked_by_loss_cooldown(
                    decision.signal,
                    safety_state=safety_state,
                    timestamp_ms=candle.close_time_ms,
                ):
                    reason = safety_state.reason(candle.close_time_ms)
                    decision = RiskDecision(
                        approved=False,
                        reason=reason,
                        signal=replace(
                            decision.signal, 
                            metadata={**decision.signal.metadata, "funnel_reason": SignalFunnelReason.COOLDOWN_BLOCKED.value}
                        ),
                    )
                    safety_state.record_blocked(reason)

                if _should_defer_decision(decision):
                    pending_decisions.append(
                        _PendingDecision(
                            decision=decision,
                            generated_at_ms=candle.close_time_ms,
                        )
                    )
                    continue
                report = execution.process_decision(
                    decision,
                    market_price=candle.close,
                    timestamp_ms=candle.close_time_ms,
                )
                reports.append(report)
                _record_report(
                    report,
                    trades=trades,
                    open_trade_legs=open_trade_legs,
                    daily_net_pnl=daily_net_pnl,
                    safety_state=safety_state,
                )
                if not report.accepted:
                    continue
                if _daily_loss_kill_switch_reached(
                    broker=broker,
                    candle=candle,
                    day_start_equity=daily_start_equity[day],
                    risk_manager=self.risk_manager,
                ):
                    reports.extend(
                        _force_close_open_positions(
                            execution=execution,
                            broker=broker,
                            candle=candle,
                            reason="Max daily loss kill switch triggered.",
                            trades=trades,
                            open_trade_legs=open_trade_legs,
                            daily_net_pnl=daily_net_pnl,
                            safety_state=safety_state,
                        )
                    )
                    halted_days.add(day)
                    break

            if day not in halted_days and (
                self.config.atr_dynamic_exits_enabled or self.config.bb_trail_enabled
            ):
                broker.update_dynamic_atr_exits(
                    candle,
                    atr=indicators.atr,
                    stop_multiple=self.config.atr_stop_multiple,
                    take_profit_multiple=self.config.atr_take_profit_multiple,
                    trailing_multiple=self.config.atr_trailing_multiple,
                    stop_enabled=self.config.atr_stop_enabled,
                    take_profit_enabled=self.config.atr_take_profit_enabled,
                    trailing_enabled=self.config.atr_trailing_enabled,
                    take_profit_mode=self.config.atr_take_profit_mode,
                    breakeven_enabled=self.config.breakeven_enabled,
                    breakeven_activation_r=self.config.breakeven_activation_r,
                    breakeven_offset_r=self.config.breakeven_offset_r,
                    profit_lock_enabled=self.config.profit_lock_enabled,
                    profit_lock_activation_r=self.config.profit_lock_activation_r,
                    profit_lock_r=self.config.profit_lock_r,
                    atr_trail_after_r_enabled=self.config.atr_trail_after_r_enabled,
                    atr_trail_activation_r=self.config.atr_trail_activation_r,
                    bb_band=indicators.bollinger,
                    bb_trail_enabled=self.config.bb_trail_enabled,
                    bb_trail_buffer_multiplier=self.config.bb_trail_buffer_multiplier,
                    bb_trail_activation_r=self.config.bb_trail_activation_r,
                    bb_trail_stage2_r=self.config.bb_trail_stage2_r,
                    bb_trail_stage3_r=self.config.bb_trail_stage3_r,
                    bb_trail_force_close_r=self.config.bb_trail_force_close_r,
                    bb_trail_partial_close_at_tp=self.config.bb_trail_partial_close_at_tp,
                    bb_trail_partial_close_pct=self.config.bb_trail_partial_close_pct,
                )

            equity_curve.append(_equity_point(broker, candle))

        if pending_decisions:
            final_candle = all_candles[-1]
            reports.extend(
                _reject_unfilled_pending_decisions(
                    pending_decisions,
                    execution=execution,
                    candle=final_candle,
                )
            )
            pending_decisions.clear()

        if broker.open_positions():
            final_candle = all_candles[-1]
            reports.extend(
                _force_close_open_positions(
                    execution=execution,
                    broker=broker,
                    candle=final_candle,
                    reason="Backtest ended; closing open paper position.",
                    trades=trades,
                    open_trade_legs=open_trade_legs,
                    daily_net_pnl=daily_net_pnl,
                    safety_state=safety_state,
                )
            )
            if equity_curve:
                equity_curve[-1] = _equity_point(broker, final_candle)

        trades = _annotate_trade_diagnostics(trades, all_candles)
        final_account = broker.snapshot({self.config.pair: all_candles[-1].close})
        metrics = compute_backtest_metrics(
            config=self.config,
            final_account=final_account,
            equity_curve=equity_curve,
            trades=trades,
            max_daily_loss_hit_count=max_daily_loss_hit_count,
            trades_blocked_by_profit_lock_or_daily_loss=trades_blocked_by_profit_lock_or_daily_loss,
        )
        return BacktestResult(
            config=self.config,
            candles_loaded=len(all_candles),
            candles_used=len(series),
            metrics=metrics,
            final_account=final_account,
            equity_curve=equity_curve,
            trades=trades,
            orders=list(broker.orders),
            fills=list(broker.fills),
            reports=reports,
        )

    def _run_intrabar(
        self,
        all_candles: list[OHLCVCandle],
        execution_candles: Iterable[OHLCVCandle],
    ) -> BacktestResult:
        execution_list = sorted(execution_candles, key=lambda candle: candle.open_time_ms)
        if not execution_list:
            raise ValueError("Intrabar backtest requires at least one execution candle.")

        execution_interval = self.config.execution_interval or execution_list[0].interval
        _validate_execution_interval(self.config.interval, execution_interval)

        broker = PaperBroker(
            starting_equity=self.config.starting_equity,
            maker_fee_rate=self.config.maker_fee_rate,
            taker_fee_rate=self.config.taker_fee_rate,
            fee_gst_rate=self.config.fee_gst_rate,
            entry_fee_type=self.config.entry_fee_type,
            exit_fee_type=self.config.exit_fee_type,
            slippage_pct=self.config.slippage_pct,
            stop_slippage_pct=self.config.stop_slippage_pct,
            trailing_stop_enabled=self.config.trailing_stop_enabled,
            trailing_stop_activation_pct=self.config.trailing_stop_activation_pct,
            trailing_stop_distance_pct=self.config.trailing_stop_distance_pct,
            quote_to_margin_rate=self.config.quote_to_margin_rate,
            unit_contract_value=self.config.unit_contract_value,
            account_currency=self.config.margin_currency,
            price_quote_currency=self.config.price_quote_currency,
        )
        broker.profit_lock_enabled = self.config.profit_locking_enabled
        broker.auto_lock_profit_pct = self.config.auto_lock_profit_pct

        execution = PaperExecutionEngine(broker)
        series = CandleSeries(maxlen=max(len(all_candles), 1))
        reports: list[PaperExecutionReport] = []
        trades: list[BacktestTrade] = []
        equity_curve: list[BacktestEquityPoint] = []
        open_trade_legs: dict[str, _OpenTradeLeg] = {}
        pending_decisions: list[_PendingDecision] = []
        daily_net_pnl: dict[str, Decimal] = {}
        daily_start_equity: dict[str, Decimal] = {}
        halted_days: set[str] = set()
        
        max_daily_loss_hit_count = 0
        trades_blocked_by_profit_lock_or_daily_loss = 0

        safety_state = _BacktestSafetyState(
            config=self.config,
            interval_ms=interval_to_ms(self.config.interval),
            peak_equity=self.config.starting_equity,
        )

        child_by_parent = _execution_candles_by_parent(all_candles, execution_list)

        for parent_candle in all_candles:
            day = _day_key(parent_candle.close_time_ms)
            if day not in daily_start_equity:
                daily_start_equity[day] = broker.snapshot(
                    {self.config.pair: parent_candle.open}
                ).tradable_base

            parent_reentries = 0
            parent_breakout_entries = 0
            reentry_cooldown_until_ms = 0
            active_bias: StrategySignal | None = None
            child_candles = child_by_parent.get(parent_candle.open_time_ms) or [parent_candle]
            parent_execution_history: list[OHLCVCandle] = []

            for child in child_candles:
                parent_execution_history.append(child)
                if pending_decisions:
                    before_reports = len(reports)
                    pending_decisions = _fill_pending_decisions(
                        pending_decisions,
                        execution=execution,
                        candle=child,
                        reports=reports,
                        trades=trades,
                        open_trade_legs=open_trade_legs,
                        daily_net_pnl=daily_net_pnl,
                        safety_state=safety_state,
                    )
                    for report in reports[before_reports:]:
                        active_bias = _entry_bias_from_report(report, active_bias)

                before_trades = len(trades)
                for report in execution.process_candle(child):
                    reports.append(report)
                    _record_report(
                        report,
                        trades=trades,
                        open_trade_legs=open_trade_legs,
                        daily_net_pnl=daily_net_pnl,
                        safety_state=safety_state,
                    )
                closed_trades = trades[before_trades:]
                if closed_trades:
                    reentry_cooldown_until_ms = max(
                        reentry_cooldown_until_ms,
                        child.close_time_ms
                        + (
                            self.config.reentry_cooldown_candles
                            * interval_to_ms(execution_interval)
                        ),
                    )

                # Update safety state with latest equity
                current_snapshot = broker.snapshot({self.config.pair: child.close})
                safety_state.observe_candle(child, current_snapshot.equity)

                if _daily_loss_kill_switch_reached(
                    broker=broker,
                    candle=child,
                    day_start_equity=daily_start_equity[day],
                    risk_manager=self.risk_manager,
                ):
                    max_daily_loss_hit_count += 1
                    reports.extend(
                        _force_close_open_positions(
                            execution=execution,
                            broker=broker,
                            candle=child,
                            reason="Max daily loss kill switch triggered.",
                            trades=trades,
                            open_trade_legs=open_trade_legs,
                            daily_net_pnl=daily_net_pnl,
                            safety_state=safety_state,
                        )
                    )
                    halted_days.add(day)
                    break

                # Task 1: Check for breakout entries in early children
                if (
                    day not in halted_days
                    and self.config.intrabar_reversal_breakout_enabled
                    and self.config.strategy_name == "hybrid_meta_v2"
                    and parent_breakout_entries < 1
                    and not broker.open_positions()
                    and not pending_decisions
                    and not safety_state.active(child.close_time_ms)
                ):
                    # Task 11: Parity - strategy needs current child candle as 'latest'
                    provisional_series = series.copy()
                    provisional_series.add(child)
                    
                    indicators = latest_indicator_snapshot(
                        provisional_series,
                        atr_period=self.config.atr_period,
                    )
                    prev_parent = series.latest() # 'series' still only has closed parents
                    features = _strategy_features(
                        broker,
                        self.config.pair,
                        self.config,
                        open_interest=self.open_interest_features.latest_at_or_before(
                            child.close_time_ms
                        ),
                        execution_candles=parent_execution_history,
                        execution_interval=execution_interval,
                        previous_parent_high=prev_parent.high if prev_parent else None,
                        previous_parent_low=prev_parent.low if prev_parent else None,
                    )
                    context = StrategyContext(
                        pair=self.config.pair,
                        interval=self.config.interval,
                        candles=provisional_series,
                        indicators=indicators,
                        features=features,
                    )
                    for signal in self.strategy_engine.evaluate(context):
                        if signal.metadata.get("entry_type") not in {"intrabar_reversal_breakout", "balanced_breakout", "pullback_continuation"}:
                            continue
                        signal = _apply_exit_overrides(
                            signal,
                            self.config,
                            current_atr=indicators.atr,
                        )
                        snapshot = broker.snapshot({self.config.pair: child.close})
                        decision = self.risk_manager.evaluate_signal(
                            signal,
                            account_equity=_risk_sizing_equity(
                                snapshot=snapshot,
                                config=self.config,
                            ),
                            available_equity=snapshot.tradable_equity if self.config.profit_locking_enabled else snapshot.equity,
                            risk_base_mode=_risk_base_mode(self.config),
                            open_positions=_risk_open_positions(broker),
                            daily_realized_pnl=-snapshot.daily_loss_from_tradable_base if self.config.profit_locking_enabled else daily_net_pnl.get(_day_key(child.close_time_ms), Decimal("0")),
                            daily_loss_limit_equity=snapshot.tradable_base if self.config.profit_locking_enabled else daily_start_equity[day],
                            instrument=self.instrument,
                            requested_leverage=self.config.leverage,
                            quote_to_margin_rate=self.config.quote_to_margin_rate,
                            unit_contract_value=self.config.unit_contract_value,
                            trading_mode="paper",
                            live_trading_enabled=False,
                            protected_profit_override_enabled=snapshot.protected_profit_override_enabled,
                        )
                        if not decision.approved and ("Daily loss reached" in decision.reason or "profit lock" in decision.reason.lower()):
                            trades_blocked_by_profit_lock_or_daily_loss += 1
                        decision = _apply_entry_safety_filter(
                            decision,
                            series=series,
                            latest_candle=child,
                            indicators=indicators,
                            config=self.config,
                        )
                        if decision.approved:
                            report = execution.process_decision(
                                decision,
                                market_price=child.close,
                                timestamp_ms=child.close_time_ms,
                            )
                            reports.append(report)
                            _record_report(
                                report,
                                trades=trades,
                                open_trade_legs=open_trade_legs,
                                daily_net_pnl=daily_net_pnl,
                                safety_state=safety_state,
                            )
                            if report.accepted:
                                parent_breakout_entries += 1
                                active_bias = _entry_bias_from_report(report, active_bias)
                        break

                if (
                    day not in halted_days
                    and active_bias is not None
                    and self.config.intrabar_reentry_enabled
                    and parent_reentries < self.config.max_reentries_per_candle
                    and not broker.open_positions()
                    and child.close_time_ms >= reentry_cooldown_until_ms
                    and not safety_state.active(child.close_time_ms)
                    and _favorable_reentry_candle(child, active_bias)
                ):
                    parent_reentries += 1
                    reentry_signal = _intrabar_reentry_signal(
                        active_bias,
                        candle=child,
                        reentry_count=parent_reentries,
                        parent_interval=self.config.interval,
                        execution_interval=execution_interval,
                    )
                    snapshot = broker.snapshot({self.config.pair: child.close})
                    decision = self.risk_manager.evaluate_signal(
                        reentry_signal,
                        account_equity=_risk_sizing_equity(
                            snapshot=snapshot,
                            config=self.config,
                        ),
                        available_equity=snapshot.tradable_equity if self.config.profit_locking_enabled else snapshot.equity,
                        risk_base_mode=_risk_base_mode(self.config),
                        open_positions=_risk_open_positions(broker),
                        daily_realized_pnl=-snapshot.daily_loss_from_tradable_base if self.config.profit_locking_enabled else daily_net_pnl.get(_day_key(child.close_time_ms), Decimal("0")),
                        daily_loss_limit_equity=snapshot.tradable_base if self.config.profit_locking_enabled else daily_start_equity[day],
                        instrument=self.instrument,
                        requested_leverage=self.config.leverage,
                        quote_to_margin_rate=self.config.quote_to_margin_rate,
                        unit_contract_value=self.config.unit_contract_value,
                        trading_mode="paper",
                        live_trading_enabled=False,
                        protected_profit_override_enabled=snapshot.protected_profit_override_enabled,
                    )
                    if not decision.approved and ("Daily loss reached" in decision.reason or "profit lock" in decision.reason.lower()):
                        trades_blocked_by_profit_lock_or_daily_loss += 1
                    decision = _apply_entry_safety_filter(
                        decision,
                        series=series,
                        latest_candle=child,
                        indicators=latest_indicator_snapshot(
                            series,
                            atr_period=self.config.atr_period,
                        ),
                        config=self.config,
                    )
                    if _entry_blocked_by_loss_cooldown(
                        decision.signal,
                        safety_state=safety_state,
                        timestamp_ms=child.close_time_ms,
                    ):
                        reason = safety_state.reason(child.close_time_ms)
                        decision = RiskDecision(
                            approved=False,
                            reason=reason,
                            signal=replace(
                                decision.signal, 
                                metadata={**decision.signal.metadata, "funnel_reason": SignalFunnelReason.COOLDOWN_BLOCKED.value}
                            ),
                        )
                        safety_state.record_blocked(reason)
                    if _should_defer_decision(decision):
                        pending_decisions.append(
                            _PendingDecision(
                                decision=decision,
                                generated_at_ms=child.close_time_ms,
                            )
                        )
                    else:
                        report = execution.process_decision(
                            decision,
                            market_price=child.close,
                            timestamp_ms=child.close_time_ms,
                        )
                        reports.append(report)
                        _record_report(
                            report,
                            trades=trades,
                            open_trade_legs=open_trade_legs,
                            daily_net_pnl=daily_net_pnl,
                            safety_state=safety_state,
                        )
            funding_paid = _apply_funding_if_due(
                broker=broker,
                config=self.config,
                candle=parent_candle,
            )
            if funding_paid != 0:
                daily_net_pnl[day] = daily_net_pnl.get(day, Decimal("0")) - funding_paid

            series.add(parent_candle)
            if day not in halted_days:
                indicators = latest_indicator_snapshot(
                    series,
                    atr_period=self.config.atr_period,
                )
                context = StrategyContext(
                    pair=self.config.pair,
                    interval=self.config.interval,
                    candles=series,
                    indicators=indicators,
                    features=_strategy_features(
                        broker,
                        self.config.pair,
                        self.config,
                        open_interest=self.open_interest_features.latest_at_or_before(
                            parent_candle.close_time_ms
                        ),
                    ),
                )
                for signal in self.strategy_engine.evaluate(context):
                    signal = _apply_exit_overrides(
                        signal,
                        self.config,
                        current_atr=indicators.atr,
                    )
                    snapshot = broker.snapshot({self.config.pair: parent_candle.close})
                    decision = self.risk_manager.evaluate_signal(
                        signal,
                        account_equity=_risk_sizing_equity(
                            snapshot=snapshot,
                            config=self.config,
                        ),
                        available_equity=snapshot.tradable_equity if self.config.profit_locking_enabled else snapshot.equity,
                        risk_base_mode=_risk_base_mode(self.config),
                        open_positions=_risk_open_positions(broker),
                        daily_realized_pnl=-snapshot.daily_loss_from_tradable_base if self.config.profit_locking_enabled else daily_net_pnl.get(_day_key(parent_candle.close_time_ms), Decimal("0")),
                        daily_loss_limit_equity=snapshot.tradable_base if self.config.profit_locking_enabled else daily_start_equity[day],
                        instrument=self.instrument,
                        requested_leverage=self.config.leverage,
                        quote_to_margin_rate=self.config.quote_to_margin_rate,
                        unit_contract_value=self.config.unit_contract_value,
                        trading_mode="paper",
                        live_trading_enabled=False,
                        protected_profit_override_enabled=snapshot.protected_profit_override_enabled,
                    )
                    if not decision.approved and ("Daily loss reached" in decision.reason or "profit lock" in decision.reason.lower()):
                        trades_blocked_by_profit_lock_or_daily_loss += 1
                    decision = _apply_entry_safety_filter(
                        decision,
                        series=series,
                        latest_candle=parent_candle,
                        indicators=indicators,
                        config=self.config,
                    )
                    if _entry_blocked_by_loss_cooldown(
                        decision.signal,
                        safety_state=safety_state,
                        timestamp_ms=parent_candle.close_time_ms,
                    ):
                        reason = safety_state.reason(parent_candle.close_time_ms)
                        decision = RiskDecision(
                            approved=False,
                            reason=reason,
                            signal=replace(
                                decision.signal, 
                                metadata={**decision.signal.metadata, "funnel_reason": SignalFunnelReason.COOLDOWN_BLOCKED.value}
                            ),
                        )
                        safety_state.record_blocked(reason)
                    if _should_defer_decision(decision):
                        pending_decisions.append(
                            _PendingDecision(
                                decision=decision,
                                generated_at_ms=parent_candle.close_time_ms,
                            )
                        )
                    else:
                        report = execution.process_decision(
                            decision,
                            market_price=parent_candle.close,
                            timestamp_ms=parent_candle.close_time_ms,
                        )
                        reports.append(report)
                        _record_report(
                            report,
                            trades=trades,
                            open_trade_legs=open_trade_legs,
                            daily_net_pnl=daily_net_pnl,
                            safety_state=safety_state,
                        )
                if self.config.atr_dynamic_exits_enabled or self.config.bb_trail_enabled:
                    broker.update_dynamic_atr_exits(
                        parent_candle,
                        atr=indicators.atr,
                        stop_multiple=self.config.atr_stop_multiple,
                        take_profit_multiple=self.config.atr_take_profit_multiple,
                        trailing_multiple=self.config.atr_trailing_multiple,
                        stop_enabled=self.config.atr_stop_enabled,
                        take_profit_enabled=self.config.atr_take_profit_enabled,
                        trailing_enabled=self.config.atr_trailing_enabled,
                        take_profit_mode=self.config.atr_take_profit_mode,
                        breakeven_enabled=self.config.breakeven_enabled,
                        breakeven_activation_r=self.config.breakeven_activation_r,
                        breakeven_offset_r=self.config.breakeven_offset_r,
                        profit_lock_enabled=self.config.profit_lock_enabled,
                        profit_lock_activation_r=self.config.profit_lock_activation_r,
                        profit_lock_r=self.config.profit_lock_r,
                        atr_trail_after_r_enabled=self.config.atr_trail_after_r_enabled,
                        atr_trail_activation_r=self.config.atr_trail_activation_r,
                        bb_band=indicators.bollinger,
                        bb_trail_enabled=self.config.bb_trail_enabled,
                        bb_trail_buffer_multiplier=self.config.bb_trail_buffer_multiplier,
                        bb_trail_activation_r=self.config.bb_trail_activation_r,
                        bb_trail_stage2_r=self.config.bb_trail_stage2_r,
                        bb_trail_stage3_r=self.config.bb_trail_stage3_r,
                        bb_trail_force_close_r=self.config.bb_trail_force_close_r,
                        bb_trail_partial_close_at_tp=self.config.bb_trail_partial_close_at_tp,
                        bb_trail_partial_close_pct=self.config.bb_trail_partial_close_pct,
                    )

            equity_curve.append(_equity_point(broker, parent_candle))

        if pending_decisions:
            final_candle = all_candles[-1]
            reports.extend(
                _reject_unfilled_pending_decisions(
                    pending_decisions,
                    execution=execution,
                    candle=final_candle,
                )
            )
            pending_decisions.clear()

        if broker.open_positions():
            final_candle = all_candles[-1]
            reports.extend(
                _force_close_open_positions(
                    execution=execution,
                    broker=broker,
                    candle=final_candle,
                    reason="Backtest ended; closing open paper position.",
                    trades=trades,
                    open_trade_legs=open_trade_legs,
                    daily_net_pnl=daily_net_pnl,
                    safety_state=safety_state,
                )
            )
            if equity_curve:
                equity_curve[-1] = _equity_point(broker, final_candle)

        diagnostic_candles = execution_list if execution_list else all_candles
        trades = _annotate_trade_diagnostics(trades, diagnostic_candles)
        final_account = broker.snapshot({self.config.pair: all_candles[-1].close})
        metrics = compute_backtest_metrics(
            config=self.config,
            final_account=final_account,
            equity_curve=equity_curve,
            trades=trades,
            max_daily_loss_hit_count=max_daily_loss_hit_count,
            trades_blocked_by_profit_lock_or_daily_loss=trades_blocked_by_profit_lock_or_daily_loss,
        )
        return BacktestResult(
            config=self.config,
            candles_loaded=len(all_candles),
            candles_used=len(series),
            metrics=metrics,
            final_account=final_account,
            equity_curve=equity_curve,
            trades=trades,
            orders=list(broker.orders),
            fills=list(broker.fills),
            reports=reports,
        )


def _should_defer_decision(decision: RiskDecision) -> bool:
    if not decision.approved:
        return False
    return decision.signal.action in {
        SignalAction.ENTER_LONG,
        SignalAction.ENTER_SHORT,
        SignalAction.EXIT_LONG,
        SignalAction.EXIT_SHORT,
    }


def _entry_blocked_by_loss_cooldown(
    signal: StrategySignal,
    *,
    safety_state: _BacktestSafetyState,
    timestamp_ms: int,
) -> bool:
    if signal.action not in {SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT}:
        return False
    return safety_state.active(timestamp_ms)


def _apply_entry_safety_filter(
    decision: RiskDecision,
    *,
    series: CandleSeries,
    latest_candle: OHLCVCandle,
    indicators,
    config: BacktestConfig,
) -> RiskDecision:
    if not decision.approved:
        return decision
    signal = decision.signal
    if signal.action not in {SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT}:
        return decision

    atr = getattr(indicators, "atr", None)
    reason = _entry_safety_rejection_reason(
        signal,
        series=series,
        latest_candle=latest_candle,
        atr=atr,
        config=config,
    )
    if reason is None:
        return decision
    safe_signal = replace(
        signal,
        metadata={
            **signal.metadata,
            "entry_safety_filter": True,
            "entry_safety_rejection": reason,
        },
    )
    return RiskDecision(approved=False, reason=reason, signal=safe_signal)


def _entry_safety_rejection_reason(
    signal: StrategySignal,
    *,
    series: CandleSeries,
    latest_candle: OHLCVCandle,
    atr: Decimal | None,
    config: BacktestConfig,
) -> str | None:
    direction = signal.direction
    if direction is None or atr is None or atr <= 0:
        return None

    # Task 6: Chop/regime filter
    if config.chop_filter_enabled:
        if config.block_low_atr_enabled:
            atr_ratio = (atr / latest_candle.close) if latest_candle.close > 0 else Decimal("0")
            if atr_ratio < config.min_atr_pct:
                return "atr_too_low"
        
        if config.block_flat_ema_enabled:
            fast = signal.metadata.get("ema_fast")
            slow = signal.metadata.get("ema_slow")
            if isinstance(fast, (Decimal, float, int)) and isinstance(slow, (Decimal, float, int)):
                fast_dec = Decimal(str(fast))
                slow_dec = Decimal(str(slow))
                gap_ratio = abs(fast_dec - slow_dec) / latest_candle.close if latest_candle.close > 0 else Decimal("0")
                if gap_ratio < config.min_ema_gap_pct:
                    return "ema_gap_too_small"

    if not config.atr_entry_filter_enabled:
        return _component_agreement_rejection(signal, config=config)

    is_reversal_breakout = signal.metadata.get("entry_type") in {
        "intrabar_reversal_breakout",
        "balanced_breakout",
        "pullback_continuation",
    }
    candle_range = latest_candle.high - latest_candle.low
    if not is_reversal_breakout and candle_range > atr * ENTRY_SAFETY_SPIKE_ATR_MULTIPLE:
        return (
            "Entry safety blocked signal: latest candle range is too large "
            "versus ATR."
        )

    opposite_rejection = _large_opposite_candle_rejection(
        direction,
        latest_candle=latest_candle,
        atr=atr,
    )
    if opposite_rejection is not None:
        return opposite_rejection

    exhaustion_rejection = _trend_exhaustion_rejection(
        direction,
        series=series,
        latest_candle=latest_candle,
        atr=atr,
    )
    if not is_reversal_breakout and exhaustion_rejection is not None:
        return exhaustion_rejection

    component_rejection = _component_agreement_rejection(signal, config=config)
    if not is_reversal_breakout and component_rejection is not None:
        return component_rejection
    if is_reversal_breakout:
        return None

    return _higher_context_rejection(
        direction,
        series=series,
        latest_candle=latest_candle,
    )


def _large_opposite_candle_rejection(
    direction: SignalDirection,
    *,
    latest_candle: OHLCVCandle,
    atr: Decimal,
) -> str | None:
    body = abs(latest_candle.close - latest_candle.open)
    if body < atr * ENTRY_SAFETY_OPPOSITE_BODY_ATR_MULTIPLE:
        return None
    if direction == SignalDirection.LONG and latest_candle.close < latest_candle.open:
        return "Entry safety blocked long: latest candle is a large red reversal."
    if direction == SignalDirection.SHORT and latest_candle.close > latest_candle.open:
        return "Entry safety blocked short: latest candle is a large green reversal."
    return None


def _trend_exhaustion_rejection(
    direction: SignalDirection,
    *,
    series: CandleSeries,
    latest_candle: OHLCVCandle,
    atr: Decimal,
) -> str | None:
    candles = list(series)
    if len(candles) <= ENTRY_SAFETY_EXHAUSTION_LOOKBACK:
        return None
    reference = candles[-(ENTRY_SAFETY_EXHAUSTION_LOOKBACK + 1)].close
    move = latest_candle.close - reference
    threshold = atr * ENTRY_SAFETY_EXHAUSTION_ATR_MULTIPLE
    if (
        direction == SignalDirection.LONG
        and move > threshold
        and latest_candle.close < latest_candle.open
    ):
        return "Entry safety blocked long: move looks exhausted after a sharp run-up."
    if (
        direction == SignalDirection.SHORT
        and -move > threshold
        and latest_candle.close > latest_candle.open
    ):
        return "Entry safety blocked short: move looks exhausted after a sharp sell-off."
    return None


def _component_agreement_rejection(
    signal: StrategySignal,
    *,
    config: BacktestConfig,
) -> str | None:
    direction = signal.direction
    if direction is None:
        return None
    scores = [
        _decimal_from_metadata(signal.metadata.get(key))
        for key in (
            "ema_score",
            "bb_score",
            "visual_score",
            "open_interest_score",
        )
    ]
    scores = [score for score in scores if score is not None]
    if len(scores) < 2:
        return None

    if direction == SignalDirection.LONG:
        agreeing = sum(1 for score in scores if score >= ENTRY_SAFETY_COMPONENT_MIN_SCORE)
        strongly_opposed = any(
            score <= -ENTRY_SAFETY_COMPONENT_STRONG_OPPOSITE for score in scores
        )
    else:
        agreeing = sum(1 for score in scores if score <= -ENTRY_SAFETY_COMPONENT_MIN_SCORE)
        strongly_opposed = any(
            score >= ENTRY_SAFETY_COMPONENT_STRONG_OPPOSITE for score in scores
        )

    if config.trade_quality_mode == "tiered":
        agreement_ratio = _metadata_decimal(
            signal.metadata,
            "agreement_ratio",
            Decimal(agreeing) / Decimal(len(scores)),
        )
        if agreement_ratio < config.b_setup_agreement_threshold:
            return "Entry safety blocked signal: strategy components do not agree enough."
        if not strongly_opposed:
            return None

    if agreeing < 2 or strongly_opposed:
        return "Entry safety blocked signal: strategy components do not agree enough."
    return None


def _higher_context_rejection(
    direction: SignalDirection,
    *,
    series: CandleSeries,
    latest_candle: OHLCVCandle,
) -> str | None:
    candles = list(series)
    group = ENTRY_SAFETY_HIGHER_CONTEXT_GROUP
    if len(candles) < group * 2:
        return None
    previous_group = candles[-(group * 2) : -group]
    if not previous_group:
        return None
    previous_close = previous_group[-1].close
    if direction == SignalDirection.LONG and latest_candle.close < previous_close:
        return "Entry safety blocked long: higher-context confirmation is bearish."
    if direction == SignalDirection.SHORT and latest_candle.close > previous_close:
        return "Entry safety blocked short: higher-context confirmation is bullish."
    return None


def _decimal_from_metadata(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _validate_execution_interval(strategy_interval: str, execution_interval: str) -> None:
    strategy_ms = interval_to_ms(strategy_interval)
    execution_ms = interval_to_ms(execution_interval)
    if execution_ms >= strategy_ms:
        raise ValueError("execution_interval must be smaller than the strategy interval.")
    if strategy_ms % execution_ms != 0:
        raise ValueError("execution_interval must divide the strategy interval cleanly.")


def _execution_candles_by_parent(
    parent_candles: list[OHLCVCandle],
    execution_candles: list[OHLCVCandle],
) -> dict[int, list[OHLCVCandle]]:
    grouped: dict[int, list[OHLCVCandle]] = {
        candle.open_time_ms: [] for candle in parent_candles
    }
    parent_index = 0
    for child in execution_candles:
        while (
            parent_index < len(parent_candles)
            and child.open_time_ms > parent_candles[parent_index].close_time_ms
        ):
            parent_index += 1
        if parent_index >= len(parent_candles):
            break
        parent = parent_candles[parent_index]
        if parent.open_time_ms <= child.open_time_ms <= parent.close_time_ms:
            grouped[parent.open_time_ms].append(child)
    return grouped


def _entry_bias_from_report(
    report: PaperExecutionReport,
    current_bias: StrategySignal | None,
) -> StrategySignal | None:
    if not report.accepted or report.risk_decision is None or report.order is None:
        return current_bias
    signal = report.risk_decision.signal
    if signal.action in {SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT}:
        return signal
    return current_bias


def _favorable_reentry_candle(
    candle: OHLCVCandle,
    bias: StrategySignal,
) -> bool:
    if bias.direction == SignalDirection.LONG:
        return candle.close > candle.open
    if bias.direction == SignalDirection.SHORT:
        return candle.close < candle.open
    return False


def _intrabar_reentry_signal(
    template: StrategySignal,
    *,
    candle: OHLCVCandle,
    reentry_count: int,
    parent_interval: str,
    execution_interval: str,
) -> StrategySignal:
    entry_price = candle.close
    stop_loss, take_profit = _retarget_exit_levels(template, entry_price)
    return replace(
        template,
        interval=execution_interval,
        timestamp_ms=candle.close_time_ms,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        reason=(
            "Intrabar re-entry from "
            f"{parent_interval} bias after lower-timeframe confirmation."
        ),
        metadata={
            **template.metadata,
            "intrabar_reentry": True,
            "intrabar_reentry_count": reentry_count,
            "parent_interval": parent_interval,
            "execution_interval": execution_interval,
            "reentry_reference_close": candle.close,
        },
    )


def _retarget_exit_levels(
    template: StrategySignal,
    entry_price: Decimal,
) -> tuple[Decimal | None, Decimal | None]:
    if template.entry_price is None or template.entry_price <= 0:
        return template.stop_loss, template.take_profit
    if template.direction is None:
        return template.stop_loss, template.take_profit

    stop_loss = template.stop_loss
    take_profit = template.take_profit
    if template.stop_loss is not None and template.stop_loss > 0:
        stop_pct = abs(template.entry_price - template.stop_loss) / template.entry_price
        if template.direction == SignalDirection.LONG:
            stop_loss = max(entry_price * (Decimal("1") - stop_pct), Decimal("0.00000001"))
        else:
            stop_loss = entry_price * (Decimal("1") + stop_pct)
    if template.take_profit is not None and template.take_profit > 0:
        target_pct = abs(template.take_profit - template.entry_price) / template.entry_price
        if template.direction == SignalDirection.LONG:
            take_profit = entry_price * (Decimal("1") + target_pct)
        else:
            take_profit = max(entry_price * (Decimal("1") - target_pct), Decimal("0.00000001"))
    return stop_loss, take_profit


def _fill_pending_decisions(
    pending_decisions: list[_PendingDecision],
    *,
    execution: PaperExecutionEngine,
    candle: OHLCVCandle,
    reports: list[PaperExecutionReport],
    trades: list[BacktestTrade],
    open_trade_legs: dict[str, _OpenTradeLeg],
    daily_net_pnl: dict[str, Decimal],
    safety_state: _BacktestSafetyState,
) -> list[_PendingDecision]:
    still_pending: list[_PendingDecision] = []
    for pending in pending_decisions:
        if pending.generated_at_ms >= candle.open_time_ms:
            still_pending.append(pending)
            continue
        report = execution.process_decision(
            pending.decision,
            market_price=candle.open,
            timestamp_ms=candle.open_time_ms,
        )
        reports.append(report)
        _record_report(
            report,
            trades=trades,
            open_trade_legs=open_trade_legs,
            daily_net_pnl=daily_net_pnl,
            safety_state=safety_state,
        )
    return still_pending


def _reject_unfilled_pending_decisions(
    pending_decisions: list[_PendingDecision],
    *,
    execution: PaperExecutionEngine,
    candle: OHLCVCandle,
) -> list[PaperExecutionReport]:
    reports: list[PaperExecutionReport] = []
    for pending in pending_decisions:
        rejected = RiskDecision(
            approved=False,
            reason="Backtest ended before next candle open; pending signal was not filled.",
            signal=pending.decision.signal,
        )
        reports.append(
            execution.process_decision(
                rejected,
                market_price=candle.close,
                timestamp_ms=candle.close_time_ms,
            )
        )
    return reports


def _risk_sizing_equity(
    *,
    snapshot: PaperAccountSnapshot,
    config: BacktestConfig,
) -> Decimal:
    if config.compound_risk_equity:
        if config.profit_locking_enabled:
            return snapshot.tradable_equity
        return snapshot.equity
    return config.starting_equity


def _risk_base_mode(config: BacktestConfig) -> str:
    return "current_equity" if config.compound_risk_equity else "initial_equity"


def _atr_execution_settings(
    signal: StrategySignal,
    config: BacktestConfig,
) -> _ATRExecutionSettings:
    use_policy = config.atr_policy_mode == "router" and isinstance(
        signal.metadata.get("atr_policy"),
        dict,
    )
    policy_mode = "router" if use_policy else "manual"
    stop_enabled = config.atr_stop_enabled
    take_profit_enabled = config.atr_take_profit_enabled
    trailing_enabled = config.atr_trailing_enabled
    stop_multiple = config.atr_stop_multiple
    take_profit_multiple = config.atr_take_profit_multiple
    trailing_multiple = config.atr_trailing_multiple
    take_profit_mode = config.atr_take_profit_mode
    risk_multiplier = Decimal("1")

    if use_policy:
        stop_enabled = _metadata_bool(signal.metadata, "atr_stop_enabled", stop_enabled)
        take_profit_enabled = _metadata_bool(
            signal.metadata,
            "atr_take_profit_enabled",
            take_profit_enabled,
        )
        trailing_enabled = _metadata_bool(
            signal.metadata,
            "atr_trailing_enabled",
            trailing_enabled,
        )
        stop_multiple = _metadata_decimal(
            signal.metadata,
            "stop_atr_multiple",
            stop_multiple,
        )
        take_profit_multiple = _metadata_decimal(
            signal.metadata,
            "take_profit_atr_multiple",
            take_profit_multiple,
        )
        trailing_multiple = _metadata_decimal(
            signal.metadata,
            "atr_trailing_multiple",
            _metadata_decimal(
                signal.metadata,
                "trailing_atr_multiple",
                trailing_multiple,
            ),
        )
        take_profit_mode = str(
            signal.metadata.get("atr_take_profit_mode", take_profit_mode)
        ).strip().lower()
        risk_multiplier = _metadata_decimal(
            signal.metadata,
            "risk_multiplier",
            risk_multiplier,
        )

    if stop_multiple <= 0:
        stop_multiple = config.atr_stop_multiple
    if take_profit_multiple <= 0:
        take_profit_multiple = config.atr_take_profit_multiple
    if trailing_multiple <= 0:
        trailing_multiple = config.atr_trailing_multiple
    if take_profit_mode not in {"fixed", "entry_atr", "ratchet", "trailing_atr", "none"}:
        take_profit_mode = config.atr_take_profit_mode
    if not take_profit_enabled:
        take_profit_mode = "none"
    if risk_multiplier <= 0:
        risk_multiplier = Decimal("1")
    risk_multiplier = min(risk_multiplier, Decimal("1"))

    return _ATRExecutionSettings(
        stop_enabled=stop_enabled,
        take_profit_enabled=take_profit_enabled,
        trailing_enabled=trailing_enabled,
        stop_multiple=stop_multiple,
        take_profit_multiple=take_profit_multiple,
        trailing_multiple=trailing_multiple,
        take_profit_mode=take_profit_mode,
        policy_mode=policy_mode,
        risk_multiplier=risk_multiplier,
    )


def _metadata_bool(
    metadata: dict[str, object],
    key: str,
    default: bool,
) -> bool:
    value = metadata.get(key)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _metadata_decimal(
    metadata: dict[str, object],
    key: str,
    default: Decimal,
) -> Decimal:
    value = metadata.get(key)
    if isinstance(value, Decimal):
        return value
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _apply_exit_overrides(
    signal: StrategySignal,
    config: BacktestConfig,
    *,
    current_atr: Decimal | None = None,
) -> StrategySignal:
    if signal.action not in {SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT}:
        return signal
    if signal.direction is None or signal.entry_price is None or signal.entry_price <= 0:
        return signal
    atr_settings = _atr_execution_settings(signal, config)
    if (
        config.atr_dynamic_exits_enabled
        and config.atr_policy_mode == "router"
        and not _metadata_bool(signal.metadata, "entry_allowed", True)
    ):
        return StrategySignal.hold(
            strategy_name=signal.strategy_name,
            pair=signal.pair,
            interval=signal.interval,
            timestamp_ms=signal.timestamp_ms,
            reason=(
                "ATR policy blocked entry: "
                f"{signal.metadata.get('atr_policy_reason', 'no policy reason')}"
            ),
            funnel_reason=SignalFunnelReason.ATR_POLICY_ENTRY_BLOCKED,
            metadata={**signal.metadata, "atr_policy_blocked_entry": True},
        )
    if (
        not config.atr_dynamic_exits_enabled
        and config.stop_loss_pct is None
        and config.take_profit_pct is None
    ):
        return signal

    stop_loss = signal.stop_loss
    take_profit = signal.take_profit
    entry_price = signal.entry_price

    if config.atr_dynamic_exits_enabled and current_atr is not None and current_atr > 0:
        if atr_settings.stop_enabled:
            stop_distance = current_atr * atr_settings.stop_multiple
            if signal.direction == SignalDirection.LONG:
                stop_loss = max(entry_price - stop_distance, Decimal("0.00000001"))
            else:
                stop_loss = entry_price + stop_distance

        if atr_settings.take_profit_enabled and atr_settings.take_profit_mode != "none":
            target_distance = current_atr * atr_settings.take_profit_multiple
            if signal.direction == SignalDirection.LONG:
                take_profit = entry_price + target_distance
            else:
                take_profit = max(entry_price - target_distance, Decimal("0.00000001"))
        elif config.take_profit_pct is None:
            take_profit = None

    if config.stop_loss_pct is None and config.take_profit_pct is None:
        if not config.atr_dynamic_exits_enabled:
            return signal
        return replace(
            signal,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata={
                **signal.metadata,
                "atr_dynamic_exits_enabled": True,
                "atr_stop_enabled": atr_settings.stop_enabled,
                "atr_take_profit_enabled": atr_settings.take_profit_enabled,
                "atr_trailing_enabled": atr_settings.trailing_enabled,
                "atr_entry_atr": current_atr,
                "atr_stop_multiple": atr_settings.stop_multiple,
                "atr_take_profit_multiple": atr_settings.take_profit_multiple,
                "atr_trailing_multiple": atr_settings.trailing_multiple,
                "trailing_atr_multiple": atr_settings.trailing_multiple,
                "atr_take_profit_mode": atr_settings.take_profit_mode,
                "atr_policy_mode": atr_settings.policy_mode,
                "risk_multiplier": atr_settings.risk_multiplier,
                "atr_best_price": entry_price,
                "exit_override_applied": True,
            },
        )

    if config.stop_loss_pct is not None:
        stop_distance = entry_price * (config.stop_loss_pct / Decimal("100"))
        if signal.direction == SignalDirection.LONG:
            stop_loss = max(entry_price - stop_distance, Decimal("0.00000001"))
        else:
            stop_loss = entry_price + stop_distance

    if config.take_profit_pct is not None:
        target_distance = entry_price * (config.take_profit_pct / Decimal("100"))
        if signal.direction == SignalDirection.LONG:
            take_profit = entry_price + target_distance
        else:
            take_profit = max(entry_price - target_distance, Decimal("0.00000001"))

    return replace(
        signal,
        stop_loss=stop_loss,
        take_profit=take_profit,
        metadata={
            **signal.metadata,
            "atr_dynamic_exits_enabled": config.atr_dynamic_exits_enabled,
            "atr_stop_enabled": atr_settings.stop_enabled,
            "atr_take_profit_enabled": atr_settings.take_profit_enabled,
            "atr_trailing_enabled": atr_settings.trailing_enabled,
            "atr_entry_atr": current_atr,
            "atr_stop_multiple": atr_settings.stop_multiple,
            "atr_take_profit_multiple": atr_settings.take_profit_multiple,
            "atr_trailing_multiple": atr_settings.trailing_multiple,
            "trailing_atr_multiple": atr_settings.trailing_multiple,
            "atr_take_profit_mode": atr_settings.take_profit_mode,
            "atr_policy_mode": atr_settings.policy_mode,
            "risk_multiplier": atr_settings.risk_multiplier,
            "atr_best_price": entry_price,
            "manual_stop_loss_pct": config.stop_loss_pct,
            "manual_take_profit_pct": config.take_profit_pct,
            "exit_override_applied": True,
        },
    )


def _daily_loss_kill_switch_reached(
    *,
    broker: PaperBroker,
    candle: OHLCVCandle,
    day_start_equity: Decimal,
    risk_manager: RiskManager,
) -> bool:
    snapshot = broker.snapshot({candle.pair: candle.close})
    if snapshot.initial_equity <= 0:
        return False
        
    # Daily loss calculated from start-of-day tradable base
    drawdown = max(Decimal("0"), day_start_equity - snapshot.tradable_equity)
    limit = daily_loss_limit_amount(snapshot.initial_equity, risk_manager.settings)
    
    return drawdown >= limit and not snapshot.protected_profit_override_enabled


def _force_close_open_positions(
    *,
    execution: PaperExecutionEngine,
    broker: PaperBroker,
    candle: OHLCVCandle,
    reason: str,
    trades: list[BacktestTrade],
    open_trade_legs: dict[str, _OpenTradeLeg],
    daily_net_pnl: dict[str, Decimal],
    safety_state: _BacktestSafetyState,
) -> list[PaperExecutionReport]:
    reports: list[PaperExecutionReport] = []
    for position in list(broker.open_positions()):
        action = (
            SignalAction.EXIT_LONG
            if position.direction == SignalDirection.LONG
            else SignalAction.EXIT_SHORT
        )
        signal = StrategySignal(
            strategy_name=position.strategy_name,
            pair=position.pair,
            interval=candle.interval,
            action=action,
            direction=position.direction,
            confidence=Decimal("1"),
            reason=reason,
            timestamp_ms=candle.close_time_ms,
            entry_price=candle.close,
            metadata={"paper_trigger": True, "forced_close": True},
        )
        decision = RiskDecision(approved=True, reason=reason, signal=signal)
        report = execution.process_decision(
            decision,
            market_price=candle.close,
            timestamp_ms=candle.close_time_ms,
        )
        reports.append(report)
        _record_report(
            report,
            trades=trades,
            open_trade_legs=open_trade_legs,
            daily_net_pnl=daily_net_pnl,
            safety_state=safety_state,
        )
    return reports


def _record_report(
    report: PaperExecutionReport,
    *,
    trades: list[BacktestTrade],
    open_trade_legs: dict[str, _OpenTradeLeg],
    daily_net_pnl: dict[str, Decimal],
    safety_state: _BacktestSafetyState,
) -> BacktestTrade | None:
    if not report.accepted or report.order is None or report.fill is None:
        return None

    _add_daily_net_pnl(daily_net_pnl, report.fill)

    if report.order.action in {SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT}:
        leg = open_trade_legs.get(report.order.pair)
        if leg is None:
            open_trade_legs[report.order.pair] = _OpenTradeLeg(entry_fee=report.fill.fee)
        else:
            leg.entry_fee += report.fill.fee
        return None

    if report.order.action not in {SignalAction.EXIT_LONG, SignalAction.EXIT_SHORT}:
        return None
    if report.position is None:
        return None

    entry_leg = open_trade_legs.pop(report.order.pair, _OpenTradeLeg(Decimal("0")))
    fees = entry_leg.entry_fee + report.fill.fee
    gross_pnl = report.fill.realized_pnl
    
    # Calculate additional metrics
    pos = report.position
    entry_notional = pos.notional
    exit_notional = (
        report.fill.quantity 
        * report.fill.price 
        * pos.unit_contract_value 
        * pos.quote_to_margin_rate
    )
    leverage = pos.leverage
    margin_used = entry_notional / leverage if leverage > 0 else Decimal("0")
    net_pnl = gross_pnl - fees
    
    net_pct_of_notional = (net_pnl / entry_notional * 100) if entry_notional > 0 else Decimal("0")
    gross_roe_pct = (gross_pnl / margin_used * 100) if margin_used > 0 else Decimal("0")
    net_roe_pct = (net_pnl / margin_used * 100) if margin_used > 0 else Decimal("0")
    
    account_equity_at_entry = _decimal_metadata(pos.metadata.get("account_equity_at_entry")) or Decimal("0")
    account_impact_pct = (net_pnl / account_equity_at_entry * 100) if account_equity_at_entry > 0 else Decimal("0")

    # Collate metadata from position and report
    metadata = {
        **pos.metadata,
        **(report.signal.metadata if report.signal is not None else {}),
        **report.fill.metadata,
        "equity_after_trade": report.account.equity,
        "account_blown": report.account.equity <= 0,
        "available_equity": report.account.equity,
        "entry_notional": entry_notional,
        "exit_notional": exit_notional,
        "margin_used": margin_used,
        "gross_roe_pct": gross_roe_pct,
        "net_roe_pct": net_roe_pct,
        "account_impact_pct": account_impact_pct,
    }
    
    trade = BacktestTrade(
        pair=report.order.pair,
        strategy_name=pos.strategy_name,
        direction=pos.direction,
        quantity=report.fill.quantity,
        entry_price=pos.entry_price,
        exit_price=report.fill.price,
        entry_time_ms=pos.opened_at_ms,
        exit_time_ms=report.fill.timestamp_ms,
        gross_pnl=gross_pnl,
        fees=fees,
        net_pnl=net_pnl,
        exit_reason=report.reason,
        entry_notional=entry_notional,
        exit_notional=exit_notional,
        leverage=leverage,
        margin_used=margin_used,
        net_pct_of_notional=net_pct_of_notional,
        gross_roe_pct=gross_roe_pct,
        net_roe_pct=net_roe_pct,
        account_equity_at_entry=account_equity_at_entry,
        account_impact_pct=account_impact_pct,
        metadata=metadata,
    )
    trades.append(trade)
    safety_state.observe_trade(trade)
    return trade


def _annotate_trade_diagnostics(
    trades: list[BacktestTrade],
    candles: list[OHLCVCandle],
    *,
    post_exit_candles: int = 3,
) -> list[BacktestTrade]:
    if not trades or not candles:
        return trades

    ordered_candles = sorted(candles, key=lambda candle: candle.open_time_ms)
    annotated: list[BacktestTrade] = []
    for trade in trades:
        trade_candles = [
            candle
            for candle in ordered_candles
            if candle.close_time_ms >= trade.entry_time_ms
            and candle.open_time_ms <= trade.exit_time_ms
        ]
        if not trade_candles:
            annotated.append(trade)
            continue

        if trade.direction == SignalDirection.LONG:
            max_favorable = max(candle.high for candle in trade_candles) - trade.entry_price
            max_adverse = trade.entry_price - min(candle.low for candle in trade_candles)
        else:
            max_favorable = trade.entry_price - min(candle.low for candle in trade_candles)
            max_adverse = max(candle.high for candle in trade_candles) - trade.entry_price

        max_favorable = max(max_favorable, Decimal("0"))
        max_adverse = max(max_adverse, Decimal("0"))
        initial_stop = _metadata_decimal(
            trade.metadata,
            "initial_stop_loss",
            Decimal("0"),
        )
        risk_per_unit = abs(trade.entry_price - initial_stop) if initial_stop > 0 else Decimal("0")
        quote_to_margin_rate = _metadata_decimal(
            trade.metadata,
            "quote_to_margin_rate",
            Decimal("1"),
        )
        unit_contract_value = _metadata_decimal(
            trade.metadata,
            "unit_contract_value",
            Decimal("1"),
        )
        value_multiplier = quote_to_margin_rate * unit_contract_value
        risk_amount = risk_per_unit * abs(trade.quantity) * value_multiplier
        r_multiple = trade.net_pnl / risk_amount if risk_amount > 0 else None

        post_candles = [
            candle
            for candle in ordered_candles
            if candle.open_time_ms > trade.exit_time_ms
        ][:post_exit_candles]
        if post_candles:
            if trade.direction == SignalDirection.LONG:
                post_exit_favorable = max(candle.high for candle in post_candles) - trade.exit_price
            else:
                post_exit_favorable = trade.exit_price - min(candle.low for candle in post_candles)
            post_exit_favorable = max(post_exit_favorable, Decimal("0"))
        else:
            post_exit_favorable = Decimal("0")

        metadata = {
            **trade.metadata,
            "mfe": max_favorable * abs(trade.quantity) * value_multiplier,
            "mae": max_adverse * abs(trade.quantity) * value_multiplier,
            "mfe_per_unit": max_favorable,
            "mae_per_unit": max_adverse,
            "mfe_pct": _pct(max_favorable, trade.entry_price),
            "mae_pct": _pct(max_adverse, trade.entry_price),
            "r_multiple": r_multiple,
            "post_exit_favorable_move": post_exit_favorable * abs(trade.quantity) * value_multiplier,
            "post_exit_favorable_move_per_unit": post_exit_favorable,
            "post_exit_favorable_move_pct": _pct(post_exit_favorable, trade.exit_price),
            "diagnostic_candle_count": len(trade_candles),
            "post_exit_diagnostic_candles": len(post_candles),
        }
        annotated.append(replace(trade, metadata=metadata))
    return annotated


def _pct(value: Any, base: Any) -> Decimal:
    try:
        v = Decimal(str(value))
        b = Decimal(str(base))
        if b <= 0:
            return Decimal("0")
        return (v / b) * Decimal("100")
    except Exception:
        return Decimal("0")


def _is_stop_exit_reason(reason: str) -> bool:
    normalized = reason.lower()
    return "stop" in normalized


def _add_daily_net_pnl(
    daily_net_pnl: dict[str, Decimal],
    fill: PaperFill,
) -> None:
    day = _day_key(fill.timestamp_ms)
    daily_net_pnl[day] = daily_net_pnl.get(day, Decimal("0")) + fill.realized_pnl - fill.fee


def _day_key(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()


def _apply_funding_if_due(
    *,
    broker: PaperBroker,
    config: BacktestConfig,
    candle: OHLCVCandle,
) -> Decimal:
    if config.funding_fee_rate == 0:
        return Decimal("0")
    boundary_ms = _funding_boundary_ms(candle, config.funding_interval_hours)
    if boundary_ms is None:
        return Decimal("0")
    return broker.apply_funding(
        mark_prices={candle.pair: candle.close},
        funding_fee_rate=config.funding_fee_rate,
    )


def _funding_boundary_ms(
    candle: OHLCVCandle,
    funding_interval_hours: int,
) -> int | None:
    if funding_interval_hours <= 0:
        return None
    interval_ms = funding_interval_hours * 60 * 60 * 1000
    boundary_ms = candle.close_time_ms + 1
    if boundary_ms % interval_ms == 0:
        return boundary_ms
    return None


def _risk_open_positions(broker: PaperBroker) -> list[OpenPosition]:
    return [
        OpenPosition(
            pair=position.pair,
            direction=position.direction,
            quantity=position.quantity,
            entry_price=position.entry_price,
            leverage=position.leverage,
            stop_loss=position.stop_loss,
            quote_to_margin_rate=position.quote_to_margin_rate,
            unit_contract_value=position.unit_contract_value,
        )
        for position in broker.open_positions()
    ]


def _strategy_features(
    broker: PaperBroker,
    pair: str,
    config: BacktestConfig,
    *,
    open_interest: dict[str, object] | None = None,
    execution_candles: list[OHLCVCandle] | None = None,
    execution_interval: str | None = None,
    previous_parent_high: Decimal | None = None,
    previous_parent_low: Decimal | None = None,
) -> dict[str, object]:
    profile_config, pair_profile = apply_pair_profile_to_config(
        pair,
        {
            "previous_parent_high": previous_parent_high,
            "previous_parent_low": previous_parent_low,
            "risk_per_trade_pct": config.risk_per_trade_pct,
            "trade_quality_mode": config.trade_quality_mode,
            "controlled_shorts_enabled": config.controlled_shorts_enabled,
            "a_setup_score_threshold": config.a_setup_score_threshold,
            "a_setup_agreement_threshold": config.a_setup_agreement_threshold,
            "b_setup_score_threshold": config.b_setup_score_threshold,
            "b_setup_agreement_threshold": config.b_setup_agreement_threshold,
            "b_setup_risk_multiplier": config.b_setup_risk_multiplier,
            "minimum_visual_score": config.minimum_visual_score,
            "long_entry_threshold": config.long_entry_threshold,
            "short_entry_threshold": config.short_entry_threshold,
            "short_agreement_threshold": config.short_agreement_threshold,
            "trailing_stop_enabled": config.trailing_stop_enabled,
            "trailing_stop_activation_pct": config.trailing_stop_activation_pct,
            "trailing_stop_distance_pct": config.trailing_stop_distance_pct,
            "atr_dynamic_exits_enabled": config.atr_dynamic_exits_enabled,
            "atr_stop_enabled": config.atr_stop_enabled,
            "atr_take_profit_enabled": config.atr_take_profit_enabled,
            "atr_trailing_enabled": config.atr_trailing_enabled,
            "bb_trail_enabled": config.bb_trail_enabled,
            "bb_trail_buffer_multiplier": config.bb_trail_buffer_multiplier,
            "bb_trail_activation_r": config.bb_trail_activation_r,
            "bb_trail_stage2_r": config.bb_trail_stage2_r,
            "bb_trail_stage3_r": config.bb_trail_stage3_r,
            "bb_trail_force_close_r": config.bb_trail_force_close_r,
            "bb_trail_partial_close_at_tp": config.bb_trail_partial_close_at_tp,
            "bb_trail_partial_close_pct": config.bb_trail_partial_close_pct,
            "atr_entry_filter_enabled": config.atr_entry_filter_enabled,
            "atr_policy_mode": config.atr_policy_mode,
            "intrabar_reversal_breakout_enabled": (
                bool(config.paper_intrabar_enabled)
                and bool(config.intrabar_reversal_breakout_enabled)
                and config.strategy_name == "hybrid_meta_v2"
            ),
            "reversal_breakout_min_execution_candles": config.reversal_breakout_min_execution_candles,
            "reversal_breakout_volume_ratio": config.reversal_breakout_volume_ratio,
            "reversal_breakout_body_ratio": config.reversal_breakout_body_ratio,
            "reversal_breakout_close_position_ratio": config.reversal_breakout_close_position_ratio,
            "reversal_breakout_risk_multiplier": config.reversal_breakout_risk_multiplier,
            "reversal_breakout_max_extension_atr": config.reversal_breakout_max_extension_atr,
            "reversal_breakout_ignition_volume_ratio": config.reversal_breakout_ignition_volume_ratio,
            "reversal_breakout_ignition_body_ratio": config.reversal_breakout_ignition_body_ratio,
            "reversal_breakout_ignition_close_position_ratio": (
                config.reversal_breakout_ignition_close_position_ratio
            ),
            "reversal_breakout_ignition_max_extension_atr": (
                config.reversal_breakout_ignition_max_extension_atr
            ),
            "reversal_breakout_ignition_risk_multiplier": (
                config.reversal_breakout_ignition_risk_multiplier
            ),
            "reversal_breakout_breakeven_activation_r": config.reversal_breakout_breakeven_activation_r,
            "reversal_breakout_profit_lock_activation_r": config.reversal_breakout_profit_lock_activation_r,
            "reversal_breakout_profit_lock_r": config.reversal_breakout_profit_lock_r,
            "reversal_breakout_time_stop_candles": config.reversal_breakout_time_stop_candles,
            "balanced_breakout_enabled": config.balanced_breakout_enabled,
            "balanced_breakout_volume_ratio_min": config.balanced_breakout_volume_ratio_min,
            "balanced_breakout_body_ratio_min": config.balanced_breakout_body_ratio_min,
            "balanced_breakout_close_position_min": config.balanced_breakout_close_position_min,
            "balanced_breakout_max_extension_atr": config.balanced_breakout_max_extension_atr,
            "balanced_breakout_max_age_candles": config.balanced_breakout_max_age_candles,
            "balanced_breakout_risk_multiplier": config.balanced_breakout_risk_multiplier,
            "false_breakout_filter_enabled": config.false_breakout_filter_enabled,
            "false_breakout_max_wick_ratio": config.false_breakout_max_wick_ratio,
            "false_breakout_require_close_outside_parent": config.false_breakout_require_close_outside_parent,
            "late_chase_block_enabled": config.late_chase_block_enabled,
            "late_chase_max_consecutive_impulse_candles": config.late_chase_max_consecutive_impulse_candles,
            "late_chase_volume_fade_ratio": config.late_chase_volume_fade_ratio,
            "late_chase_max_extension_atr": config.late_chase_max_extension_atr,
            "pullback_entry_enabled": config.pullback_entry_enabled,
            "pullback_max_age_candles": config.pullback_max_age_candles,
            "pullback_max_distance_from_ema_atr": config.pullback_max_distance_from_ema_atr,
            "pullback_resume_body_ratio_min": config.pullback_resume_body_ratio_min,
            "pullback_risk_multiplier": config.pullback_risk_multiplier,
            "signal_flip_grace_candles": config.signal_flip_grace_candles,
            "signal_flip_confirm_candles": config.signal_flip_confirm_candles,
            "time_stop_extend_if_momentum_strong": config.time_stop_extend_if_momentum_strong,
        },
    )
    features: dict[str, object] = {
        "backtest_config": profile_config,
        "pair_profile": pair_profile.metadata(),
    }
    if execution_candles:
        features["execution_candles"] = execution_candles[-30:]
    if execution_interval:
        features["execution_interval"] = execution_interval
    if open_interest is not None:
        features["open_interest"] = open_interest
    for position in broker.open_positions():
        if position.pair != pair:
            continue
        features["open_position"] = {
            "pair": position.pair,
            "direction": position.direction.value,
            "quantity": position.quantity,
            "entry_price": position.entry_price,
            "opened_at_ms": position.opened_at_ms,
            "strategy_name": position.strategy_name,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "metadata": position.metadata,
        }
        break
    return features


def _equity_point(broker: PaperBroker, candle: OHLCVCandle) -> BacktestEquityPoint:
    snapshot = broker.snapshot({candle.pair: candle.close})
    return BacktestEquityPoint(
        timestamp_ms=candle.close_time_ms,
        close_price=candle.close,
        equity=snapshot.equity,
        realized_pnl=snapshot.realized_pnl,
        unrealized_pnl=snapshot.unrealized_pnl,
        fees_paid=snapshot.fees_paid,
        open_position_count=snapshot.open_position_count,
        open_notional=snapshot.open_notional,
        funding_paid=snapshot.funding_paid,
    )
