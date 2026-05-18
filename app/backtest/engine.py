from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from app.backtest.metrics import compute_backtest_metrics
from app.backtest.models import (
    BacktestConfig,
    BacktestEquityPoint,
    BacktestResult,
    BacktestTrade,
)
from app.broker.models import PaperExecutionReport, PaperFill, PaperOrder
from app.broker.paper import PaperBroker
from app.data.candle_builder import CandleSeries, OHLCVCandle, interval_to_ms
from app.data.indicators import latest_indicator_snapshot
from app.data.open_interest import OpenInterestFeatureSeries
from app.execution.engine import PaperExecutionEngine
from app.risk.limits import daily_loss_limit_amount
from app.risk.manager import RiskManager
from app.risk.models import InstrumentMetadata, OpenPosition, RiskDecision
from app.strategies.base import (
    SignalAction,
    SignalDirection,
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
class _LossStreakState:
    max_consecutive_losses: int
    cooldown_ms: int
    stop_cooldown_ms: int
    consecutive_losses: int = 0
    cooldown_until_ms: int = 0
    stop_cooldown_until_ms: int = 0
    blocked_entries: int = 0

    def active(self, timestamp_ms: int) -> bool:
        return timestamp_ms < max(self.cooldown_until_ms, self.stop_cooldown_until_ms)

    def reason(self, timestamp_ms: int) -> str:
        if timestamp_ms < self.stop_cooldown_until_ms:
            remaining_ms = self.stop_cooldown_until_ms - timestamp_ms
            return (
                "Stop-loss cooldown active after a losing stop exit; "
                f"remaining_ms={remaining_ms}."
            )
        remaining_ms = max(self.cooldown_until_ms - timestamp_ms, 0)
        return (
            "Loss-streak cooldown active after "
            f"{self.consecutive_losses} consecutive losing trade(s); "
            f"remaining_ms={remaining_ms}."
        )

    def observe_trade(self, trade: BacktestTrade) -> None:
        if trade.net_pnl < 0:
            if self.stop_cooldown_ms > 0 and _is_stop_exit_reason(trade.exit_reason):
                self.stop_cooldown_until_ms = max(
                    self.stop_cooldown_until_ms,
                    trade.exit_time_ms + self.stop_cooldown_ms,
                )
            if self.max_consecutive_losses <= 0 or self.cooldown_ms <= 0:
                return
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.max_consecutive_losses:
                self.cooldown_until_ms = max(
                    self.cooldown_until_ms,
                    trade.exit_time_ms + self.cooldown_ms,
                )
            return
        if trade.net_pnl > 0:
            self.consecutive_losses = 0
            if trade.exit_time_ms >= self.cooldown_until_ms:
                self.cooldown_until_ms = 0


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
        )
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
        loss_streak = _LossStreakState(
            max_consecutive_losses=self.config.max_consecutive_losses,
            cooldown_ms=self.config.loss_cooldown_candles
            * interval_to_ms(self.config.interval),
            stop_cooldown_ms=self.config.stop_loss_cooldown_candles
            * interval_to_ms(self.config.interval),
        )

        for candle in all_candles:
            day = _day_key(candle.close_time_ms)
            if day not in daily_start_equity:
                daily_start_equity[day] = broker.snapshot({self.config.pair: candle.open}).equity

            if pending_decisions:
                pending_decisions = _fill_pending_decisions(
                    pending_decisions,
                    execution=execution,
                    candle=candle,
                    reports=reports,
                    trades=trades,
                    open_trade_legs=open_trade_legs,
                    daily_net_pnl=daily_net_pnl,
                    loss_streak=loss_streak,
                )

            for report in execution.process_candle(candle):
                reports.append(report)
                _record_report(
                    report,
                    trades=trades,
                    open_trade_legs=open_trade_legs,
                    daily_net_pnl=daily_net_pnl,
                    loss_streak=loss_streak,
                )

            funding_paid = _apply_funding_if_due(
                broker=broker,
                config=self.config,
                candle=candle,
            )
            if funding_paid != 0:
                daily_net_pnl[day] = daily_net_pnl.get(day, Decimal("0")) - funding_paid

            series.add(candle)
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
                        loss_streak=loss_streak,
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
                signal = _apply_exit_overrides(
                    signal,
                    self.config,
                    current_atr=indicators.atr,
                )
                snapshot = broker.snapshot({self.config.pair: candle.close})
                risk_equity = _risk_sizing_equity(
                    snapshot_equity=snapshot.equity,
                    config=self.config,
                )
                decision = self.risk_manager.evaluate_signal(
                    signal,
                    account_equity=risk_equity,
                    available_equity=snapshot.equity,
                    risk_base_mode=_risk_base_mode(self.config),
                    open_positions=_risk_open_positions(broker),
                    daily_realized_pnl=daily_net_pnl.get(
                        _day_key(candle.close_time_ms),
                        Decimal("0"),
                    ),
                    daily_loss_limit_equity=daily_start_equity[day],
                    instrument=self.instrument,
                    requested_leverage=self.config.leverage,
                    trading_mode="paper",
                    live_trading_enabled=False,
                )
                decision = _apply_entry_safety_filter(
                    decision,
                    series=series,
                    latest_candle=candle,
                    indicators=indicators,
                    config=self.config,
                )
                if _entry_blocked_by_loss_cooldown(
                    decision.signal,
                    loss_streak=loss_streak,
                    timestamp_ms=candle.close_time_ms,
                ):
                    decision = RiskDecision(
                        approved=False,
                        reason=loss_streak.reason(candle.close_time_ms),
                        signal=decision.signal,
                    )
                    loss_streak.blocked_entries += 1
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
                    loss_streak=loss_streak,
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
                            loss_streak=loss_streak,
                        )
                    )
                    halted_days.add(day)
                    break

            if day not in halted_days and self.config.atr_dynamic_exits_enabled:
                broker.update_dynamic_atr_exits(
                    candle,
                    atr=indicators.atr,
                    stop_multiple=self.config.atr_stop_multiple,
                    take_profit_multiple=self.config.atr_take_profit_multiple,
                    stop_enabled=self.config.atr_stop_enabled,
                    take_profit_enabled=self.config.atr_take_profit_enabled,
                    trailing_enabled=self.config.atr_trailing_enabled,
                    take_profit_mode=self.config.atr_take_profit_mode,
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
                    loss_streak=loss_streak,
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
        )
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
        loss_streak = _LossStreakState(
            max_consecutive_losses=self.config.max_consecutive_losses,
            cooldown_ms=self.config.loss_cooldown_candles
            * interval_to_ms(self.config.interval),
            stop_cooldown_ms=self.config.stop_loss_cooldown_candles
            * interval_to_ms(self.config.interval),
        )

        child_by_parent = _execution_candles_by_parent(all_candles, execution_list)

        for parent_candle in all_candles:
            day = _day_key(parent_candle.close_time_ms)
            if day not in daily_start_equity:
                daily_start_equity[day] = broker.snapshot(
                    {self.config.pair: parent_candle.open}
                ).equity

            parent_reentries = 0
            reentry_cooldown_until_ms = 0
            active_bias: StrategySignal | None = None
            child_candles = child_by_parent.get(parent_candle.open_time_ms) or [parent_candle]

            for child in child_candles:
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
                        loss_streak=loss_streak,
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
                        loss_streak=loss_streak,
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

                if _daily_loss_kill_switch_reached(
                    broker=broker,
                    candle=child,
                    day_start_equity=daily_start_equity[day],
                    risk_manager=self.risk_manager,
                ):
                    reports.extend(
                        _force_close_open_positions(
                            execution=execution,
                            broker=broker,
                            candle=child,
                            reason="Max daily loss kill switch triggered.",
                            trades=trades,
                            open_trade_legs=open_trade_legs,
                            daily_net_pnl=daily_net_pnl,
                            loss_streak=loss_streak,
                        )
                    )
                    halted_days.add(day)
                    break

                if (
                    day not in halted_days
                    and active_bias is not None
                    and self.config.intrabar_reentry_enabled
                    and parent_reentries < self.config.max_reentries_per_candle
                    and not broker.open_positions()
                    and child.close_time_ms >= reentry_cooldown_until_ms
                    and not loss_streak.active(child.close_time_ms)
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
                            snapshot_equity=snapshot.equity,
                            config=self.config,
                        ),
                        available_equity=snapshot.equity,
                        risk_base_mode=_risk_base_mode(self.config),
                        open_positions=_risk_open_positions(broker),
                        daily_realized_pnl=daily_net_pnl.get(
                            _day_key(child.close_time_ms),
                            Decimal("0"),
                        ),
                        daily_loss_limit_equity=daily_start_equity[day],
                        instrument=self.instrument,
                        requested_leverage=self.config.leverage,
                        trading_mode="paper",
                        live_trading_enabled=False,
                    )
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
                        loss_streak=loss_streak,
                        timestamp_ms=child.close_time_ms,
                    ):
                        decision = RiskDecision(
                            approved=False,
                            reason=loss_streak.reason(child.close_time_ms),
                            signal=decision.signal,
                        )
                        loss_streak.blocked_entries += 1
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
                            loss_streak=loss_streak,
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
                            snapshot_equity=snapshot.equity,
                            config=self.config,
                        ),
                        available_equity=snapshot.equity,
                        risk_base_mode=_risk_base_mode(self.config),
                        open_positions=_risk_open_positions(broker),
                        daily_realized_pnl=daily_net_pnl.get(
                            _day_key(parent_candle.close_time_ms),
                            Decimal("0"),
                        ),
                        daily_loss_limit_equity=daily_start_equity[day],
                        instrument=self.instrument,
                        requested_leverage=self.config.leverage,
                        trading_mode="paper",
                        live_trading_enabled=False,
                    )
                    decision = _apply_entry_safety_filter(
                        decision,
                        series=series,
                        latest_candle=parent_candle,
                        indicators=indicators,
                        config=self.config,
                    )
                    if _entry_blocked_by_loss_cooldown(
                        decision.signal,
                        loss_streak=loss_streak,
                        timestamp_ms=parent_candle.close_time_ms,
                    ):
                        decision = RiskDecision(
                            approved=False,
                            reason=loss_streak.reason(parent_candle.close_time_ms),
                            signal=decision.signal,
                        )
                        loss_streak.blocked_entries += 1
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
                            loss_streak=loss_streak,
                        )

                if self.config.atr_dynamic_exits_enabled:
                    broker.update_dynamic_atr_exits(
                        parent_candle,
                        atr=indicators.atr,
                        stop_multiple=self.config.atr_stop_multiple,
                        take_profit_multiple=self.config.atr_take_profit_multiple,
                        stop_enabled=self.config.atr_stop_enabled,
                        take_profit_enabled=self.config.atr_take_profit_enabled,
                        trailing_enabled=self.config.atr_trailing_enabled,
                        take_profit_mode=self.config.atr_take_profit_mode,
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
                    loss_streak=loss_streak,
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
    loss_streak: _LossStreakState,
    timestamp_ms: int,
) -> bool:
    if signal.action not in {SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT}:
        return False
    return loss_streak.active(timestamp_ms)


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

    reason = _entry_safety_rejection_reason(
        signal,
        series=series,
        latest_candle=latest_candle,
        atr=indicators.atr if indicators is not None else None,
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
    if direction is None or atr is None or atr <= 0 or not config.atr_entry_filter_enabled:
        return _component_agreement_rejection(signal, config=config)

    candle_range = latest_candle.high - latest_candle.low
    if candle_range > atr * ENTRY_SAFETY_SPIKE_ATR_MULTIPLE:
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
    if exhaustion_rejection is not None:
        return exhaustion_rejection

    component_rejection = _component_agreement_rejection(signal, config=config)
    if component_rejection is not None:
        return component_rejection

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
    loss_streak: _LossStreakState,
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
            loss_streak=loss_streak,
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
    snapshot_equity: Decimal,
    config: BacktestConfig,
) -> Decimal:
    if config.compound_risk_equity:
        return snapshot_equity
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
    trailing_multiple = config.atr_stop_multiple
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
            "trailing_atr_multiple",
            trailing_multiple,
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
        trailing_multiple = stop_multiple
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
    if day_start_equity <= 0:
        return False
    snapshot = broker.snapshot({candle.pair: candle.close})
    daily_equity_pnl = snapshot.equity - day_start_equity
    if daily_equity_pnl >= 0:
        return False
    limit = daily_loss_limit_amount(day_start_equity, risk_manager.settings)
    return abs(daily_equity_pnl) >= limit


def _force_close_open_positions(
    *,
    execution: PaperExecutionEngine,
    broker: PaperBroker,
    candle: OHLCVCandle,
    reason: str,
    trades: list[BacktestTrade],
    open_trade_legs: dict[str, _OpenTradeLeg],
    daily_net_pnl: dict[str, Decimal],
    loss_streak: _LossStreakState,
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
            loss_streak=loss_streak,
        )
    return reports


def _record_report(
    report: PaperExecutionReport,
    *,
    trades: list[BacktestTrade],
    open_trade_legs: dict[str, _OpenTradeLeg],
    daily_net_pnl: dict[str, Decimal],
    loss_streak: _LossStreakState,
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
    trade = BacktestTrade(
        pair=report.order.pair,
        strategy_name=report.position.strategy_name,
        direction=report.position.direction,
        quantity=report.fill.quantity,
        entry_price=report.position.entry_price,
        exit_price=report.fill.price,
        entry_time_ms=report.position.opened_at_ms,
        exit_time_ms=report.fill.timestamp_ms,
        gross_pnl=gross_pnl,
        fees=fees,
        net_pnl=gross_pnl - fees,
        exit_reason=report.reason,
        metadata={
            **report.position.metadata,
            **(report.signal.metadata if report.signal is not None else {}),
            **report.fill.metadata,
            "equity_after_trade": report.account.equity,
            "account_blown": report.account.equity <= 0,
        },
    )
    trades.append(trade)
    loss_streak.observe_trade(trade)
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
        risk_amount = risk_per_unit * abs(trade.quantity)
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
            "mfe": max_favorable * abs(trade.quantity),
            "mae": max_adverse * abs(trade.quantity),
            "mfe_per_unit": max_favorable,
            "mae_per_unit": max_adverse,
            "mfe_pct": _pct(max_favorable, trade.entry_price),
            "mae_pct": _pct(max_adverse, trade.entry_price),
            "r_multiple": r_multiple,
            "post_exit_favorable_move": post_exit_favorable * abs(trade.quantity),
            "post_exit_favorable_move_per_unit": post_exit_favorable,
            "post_exit_favorable_move_pct": _pct(post_exit_favorable, trade.exit_price),
            "diagnostic_candle_count": len(trade_candles),
            "post_exit_diagnostic_candles": len(post_candles),
        }
        annotated.append(replace(trade, metadata=metadata))
    return annotated


def _pct(value: Decimal, base: Decimal) -> Decimal:
    if base <= 0:
        return Decimal("0")
    return (value / base) * Decimal("100")


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
        )
        for position in broker.open_positions()
    ]


def _strategy_features(
    broker: PaperBroker,
    pair: str,
    config: BacktestConfig,
    *,
    open_interest: dict[str, object] | None = None,
) -> dict[str, object]:
    features: dict[str, object] = {
        "backtest_config": {
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
            "atr_entry_filter_enabled": config.atr_entry_filter_enabled,
            "atr_policy_mode": config.atr_policy_mode,
        }
    }
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
