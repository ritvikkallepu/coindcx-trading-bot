from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.data.candle_builder import OHLCVCandle, interval_to_ms
from app.data.indicators import average_true_range, exponential_moving_average
from app.strategies.base import (
    SignalAction,
    SignalDirection,
    Strategy,
    StrategyContext,
    StrategySignal,
    clamp_confidence,
)
from app.strategies.swing_structure import find_latest_impulse


@dataclass(frozen=True)
class FibMAPullbackStrategy(Strategy):
    """Trend-pullback strategy using Fibonacci retracement zones and moving averages."""

    name: str = "fib_ma_pullback"
    fast_period: int = 50
    slow_period: int = 200
    warmup_lookback: int = 250
    atr_period: int = 14
    volume_period: int = 20
    swing_lookback: int = 80
    max_pullback_candles: int = 24
    pivot_left_bars: int = 2
    pivot_right_bars: int = 2
    fib_min: Decimal = Decimal("0.382")
    fib_max: Decimal = Decimal("0.618")
    min_swing_atr: Decimal = Decimal("2.0")
    min_volume_ratio: Decimal = Decimal("0.45")
    entry_buffer_atr: Decimal = Decimal("0.20")
    stop_buffer_atr: Decimal = Decimal("0.25")
    take_profit_extension: Decimal = Decimal("0.272")
    min_reward_r: Decimal = Decimal("1.50")
    fallback_stop_pct: Decimal = Decimal("0.015")
    intrabar_trigger_enabled: bool = True
    intrabar_min_parent_interval_ms: int = 15 * 60_000
    intrabar_min_execution_interval_ms: int = 5 * 60_000
    intrabar_min_volume_ratio: Decimal = Decimal("0.75")
    intrabar_min_body_ratio: Decimal = Decimal("0.30")
    intrabar_min_close_position: Decimal = Decimal("0.60")
    intrabar_min_rejection_wick_ratio: Decimal = Decimal("0.25")
    intrabar_max_zone_extension_atr: Decimal = Decimal("0.80")

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        latest = context.latest_candle
        if latest is None:
            return self._hold(context, "No candles available.")

        required = max(
            self.slow_period + 1,
            self.atr_period + 1,
            self.volume_period + 1,
            min(self.swing_lookback, self.slow_period) + 3,
        )
        if len(context.candles) < required:
            return self._hold(context, f"Need at least {required} candles.")

        candles = list(context.candles)
        closes = context.candles.closes()
        volumes = context.candles.volumes()
        fast_values = exponential_moving_average(closes, self.fast_period)
        slow_values = exponential_moving_average(closes, self.slow_period)
        atr_values = average_true_range(candles, self.atr_period)
        average_volume = _robust_average_volume(volumes[:-1], self.volume_period)
        if average_volume is None:
            average_volume = _robust_average_volume(volumes, self.volume_period)

        fast_current = fast_values[-1]
        fast_previous = fast_values[-2]
        slow_current = slow_values[-1]
        slow_previous = slow_values[-2]
        atr_current = atr_values[-1]
        if (
            fast_current is None
            or fast_previous is None
            or slow_current is None
            or slow_previous is None
            or atr_current is None
            or atr_current <= 0
            or average_volume is None
            or average_volume <= 0
        ):
            return self._hold(context, "Indicators are still warming up.")

        effective_volume, volume_projection_factor = _effective_latest_volume(
            latest.volume,
            context.features,
        )
        volume_ratio = effective_volume / average_volume if average_volume > 0 else Decimal("0")
        base_metadata = self._base_metadata(
            context=context,
            fast_current=fast_current,
            fast_previous=fast_previous,
            slow_current=slow_current,
            slow_previous=slow_previous,
            atr_current=atr_current,
            volume_ratio=volume_ratio,
            raw_volume=latest.volume,
            effective_volume=effective_volume,
            average_volume=average_volume,
            volume_projection_factor=volume_projection_factor,
        )

        open_position = _open_position(context.features, context.pair)
        if open_position is not None:
            exit_signal = self._evaluate_exit(
                context=context,
                open_position=open_position,
                fast_current=fast_current,
                slow_current=slow_current,
                latest=latest,
                metadata=base_metadata,
            )
            if exit_signal is not None:
                return exit_signal

        execution_trigger_enabled = self._execution_trigger_enabled(context)

        long_block = self._entry_setup(
            direction=SignalDirection.LONG,
            candles=candles,
            latest=latest,
            fast_current=fast_current,
            fast_previous=fast_previous,
            slow_current=slow_current,
            slow_previous=slow_previous,
            atr=atr_current,
            volume_ratio=volume_ratio,
            require_parent_volume=not execution_trigger_enabled,
        )
        if isinstance(long_block, _FibSetup):
            if execution_trigger_enabled:
                trigger = self._execution_trigger(
                    context=context,
                    setup=long_block,
                    atr=atr_current,
                )
                if isinstance(trigger, _FibExecutionTrigger):
                    return self._entry_signal(
                        context=context,
                        setup=long_block,
                        atr=atr_current,
                        metadata={**base_metadata, **trigger.to_metadata()},
                        reason=(
                            "Fib MA intrabar long: higher-timeframe trend and Fib zone are valid; "
                            "execution candle rejected upward from the zone."
                        ),
                        entry_candle=trigger.candle,
                        entry_type="fib_ma_intrabar_pullback",
                    )
                long_block = trigger
            else:
                parent_trigger = self._parent_rejection_trigger(
                    direction=SignalDirection.LONG,
                    setup=long_block,
                    latest=latest,
                    previous=candles[-2],
                    atr=atr_current,
                )
                if parent_trigger is None:
                    return self._entry_signal(
                        context=context,
                        setup=long_block,
                        atr=atr_current,
                        metadata=base_metadata,
                        reason="Fib MA pullback long: trend is bullish and price rejected the 0.382-0.618 retracement zone.",
                    )
                long_block = parent_trigger

        short_block = self._entry_setup(
            direction=SignalDirection.SHORT,
            candles=candles,
            latest=latest,
            fast_current=fast_current,
            fast_previous=fast_previous,
            slow_current=slow_current,
            slow_previous=slow_previous,
            atr=atr_current,
            volume_ratio=volume_ratio,
            require_parent_volume=not execution_trigger_enabled,
        )
        if isinstance(short_block, _FibSetup):
            if execution_trigger_enabled:
                trigger = self._execution_trigger(
                    context=context,
                    setup=short_block,
                    atr=atr_current,
                )
                if isinstance(trigger, _FibExecutionTrigger):
                    return self._entry_signal(
                        context=context,
                        setup=short_block,
                        atr=atr_current,
                        metadata={**base_metadata, **trigger.to_metadata()},
                        reason=(
                            "Fib MA intrabar short: higher-timeframe trend and Fib zone are valid; "
                            "execution candle rejected downward from the zone."
                        ),
                        entry_candle=trigger.candle,
                        entry_type="fib_ma_intrabar_pullback",
                    )
                short_block = trigger
            else:
                parent_trigger = self._parent_rejection_trigger(
                    direction=SignalDirection.SHORT,
                    setup=short_block,
                    latest=latest,
                    previous=candles[-2],
                    atr=atr_current,
                )
                if parent_trigger is None:
                    return self._entry_signal(
                        context=context,
                        setup=short_block,
                        atr=atr_current,
                        metadata=base_metadata,
                        reason="Fib MA pullback short: trend is bearish and price rejected the 0.382-0.618 retracement zone.",
                    )
                short_block = parent_trigger

        return self._hold(
            context,
            _combine_hold_reason(long_block, short_block),
            {
                **base_metadata,
                "entry_type": "fib_ma_pullback",
                "long_rejection": long_block,
                "short_rejection": short_block,
                "fib_intrabar_trigger_enabled": execution_trigger_enabled,
            },
        )

    def _entry_setup(
        self,
        *,
        direction: SignalDirection,
        candles: list[OHLCVCandle],
        latest: OHLCVCandle,
        fast_current: Decimal,
        fast_previous: Decimal,
        slow_current: Decimal,
        slow_previous: Decimal,
        atr: Decimal,
        volume_ratio: Decimal,
        require_parent_volume: bool,
    ) -> _FibSetup | str:
        if require_parent_volume and volume_ratio < self.min_volume_ratio:
            return f"Volume ratio below fib pullback threshold: {volume_ratio:.2f} < {self.min_volume_ratio}."

        if direction == SignalDirection.LONG:
            if fast_current <= slow_current:
                return "EMA trend is not bullish."
            if slow_current < slow_previous:
                return "Slow EMA is falling; long trend not confirmed."
            if latest.close <= slow_current:
                return "Price is below slow EMA; long pullback is too deep."
        else:
            if fast_current >= slow_current:
                return "EMA trend is not bearish."
            if slow_current > slow_previous:
                return "Slow EMA is rising; short trend not confirmed."
            if latest.close >= slow_current:
                return "Price is above slow EMA; short pullback is too deep."

        setup = _find_fib_setup(
            candles,
            direction=direction,
            lookback=self.swing_lookback,
            max_pullback_candles=self.max_pullback_candles,
            pivot_left_bars=self.pivot_left_bars,
            pivot_right_bars=self.pivot_right_bars,
            fib_min=self.fib_min,
            fib_max=self.fib_max,
            min_swing_range=atr * self.min_swing_atr,
        )
        if setup is None:
            return "No confirmed impulse swing for Fibonacci pullback."
        if setup.swing_range < atr * self.min_swing_atr:
            return f"Impulse swing too small: {setup.swing_range:.8f} < {atr * self.min_swing_atr:.8f}."

        if direction == SignalDirection.LONG:
            if latest.close >= setup.swing_high:
                return "Price already reclaimed the swing high; fresh entry would chase."
            if latest.close <= setup.swing_low:
                return "Price already broke the swing low; long setup invalidated."
            return setup

        if latest.close <= setup.swing_low:
            return "Price already broke the swing low; fresh entry would chase."
        if latest.close >= setup.swing_high:
            return "Price already reclaimed the swing high; short setup invalidated."
        return setup

    def _parent_rejection_trigger(
        self,
        *,
        direction: SignalDirection,
        setup: _FibSetup,
        latest: OHLCVCandle,
        previous: OHLCVCandle,
        atr: Decimal,
    ) -> str | None:
        buffer = atr * self.entry_buffer_atr
        if direction == SignalDirection.LONG:
            touched_zone = latest.low <= setup.zone_high + buffer and latest.close >= setup.zone_low - buffer
            if not touched_zone:
                return "Price has not pulled back into the bullish Fibonacci zone."
            if latest.close <= latest.open or latest.close < previous.close:
                return "No bullish rejection candle from the Fibonacci zone."
            return None

        touched_zone = latest.high >= setup.zone_low - buffer and latest.close <= setup.zone_high + buffer
        if not touched_zone:
            return "Price has not pulled back into the bearish Fibonacci zone."
        if latest.close >= latest.open or latest.close > previous.close:
            return "No bearish rejection candle from the Fibonacci zone."
        return None

    def _execution_trigger_enabled(self, context: StrategyContext) -> bool:
        if not self.intrabar_trigger_enabled:
            return False
        execution_interval = str(context.features.get("execution_interval") or "")
        if not execution_interval or execution_interval == context.interval:
            return False
        try:
            parent_ms = interval_to_ms(context.interval)
            execution_ms = interval_to_ms(execution_interval)
        except ValueError:
            return False
        return (
            parent_ms >= self.intrabar_min_parent_interval_ms
            and execution_ms >= self.intrabar_min_execution_interval_ms
            and execution_ms < parent_ms
        )

    def _execution_trigger(
        self,
        *,
        context: StrategyContext,
        setup: _FibSetup,
        atr: Decimal,
    ) -> _FibExecutionTrigger | str:
        latest_parent = context.latest_candle
        execution_candles = _execution_candles(context.features)
        if latest_parent is None or len(execution_candles) < 3:
            return "Waiting for enough execution candles for Fib intrabar trigger."

        trigger = execution_candles[-1]
        previous = execution_candles[-2]
        if not trigger.is_closed:
            return "Execution candle is still forming; Fib intrabar trigger waits for close."
        if trigger.close_time_ms <= latest_parent.close_time_ms:
            return "Waiting for fresh execution candle after latest higher-timeframe setup."

        average_volume = _robust_average_volume(
            [c.volume for c in execution_candles[:-1]],
            min(self.volume_period, max(1, len(execution_candles) - 1)),
        )
        if average_volume is None or average_volume <= 0:
            return "Execution volume baseline unavailable for Fib intrabar trigger."
        volume_ratio = trigger.volume / average_volume
        if volume_ratio < self.intrabar_min_volume_ratio:
            return (
                "Execution volume ratio below Fib trigger threshold: "
                f"{volume_ratio:.2f} < {self.intrabar_min_volume_ratio}."
            )

        range_size = trigger.high - trigger.low
        if range_size <= 0:
            return "Execution candle has no range for Fib intrabar trigger."
        body_ratio = abs(trigger.close - trigger.open) / range_size
        if body_ratio < self.intrabar_min_body_ratio:
            return (
                "Execution candle body too weak for Fib trigger: "
                f"{body_ratio:.2f} < {self.intrabar_min_body_ratio}."
            )

        buffer = atr * self.entry_buffer_atr
        extension_limit = atr * self.intrabar_max_zone_extension_atr
        if setup.direction == SignalDirection.LONG:
            touched_zone = trigger.low <= setup.zone_high + buffer and trigger.close >= setup.zone_low - buffer
            if not touched_zone:
                return "Execution price has not pulled back into the bullish Fibonacci zone."
            if trigger.close <= trigger.open or trigger.close <= previous.close:
                return "No intrabar bullish rejection from the Fibonacci zone."
            if trigger.close >= setup.swing_high:
                return "Intrabar long trigger already reclaimed the swing high; fresh entry would chase."
            if trigger.close - setup.zone_high > extension_limit:
                return "Intrabar long trigger is too extended from the Fibonacci zone."
            close_position = (trigger.close - trigger.low) / range_size
            lower_wick_ratio = (min(trigger.open, trigger.close) - trigger.low) / range_size
            if (
                close_position < self.intrabar_min_close_position
                or lower_wick_ratio < self.intrabar_min_rejection_wick_ratio
            ):
                return "Intrabar long trigger lacks strong lower-wick rejection."
        else:
            touched_zone = trigger.high >= setup.zone_low - buffer and trigger.close <= setup.zone_high + buffer
            if not touched_zone:
                return "Execution price has not pulled back into the bearish Fibonacci zone."
            if trigger.close >= trigger.open or trigger.close >= previous.close:
                return "No intrabar bearish rejection from the Fibonacci zone."
            if trigger.close <= setup.swing_low:
                return "Intrabar short trigger already broke the swing low; fresh entry would chase."
            if setup.zone_low - trigger.close > extension_limit:
                return "Intrabar short trigger is too extended from the Fibonacci zone."
            close_position = (trigger.high - trigger.close) / range_size
            upper_wick_ratio = (trigger.high - max(trigger.open, trigger.close)) / range_size
            if (
                close_position < self.intrabar_min_close_position
                or upper_wick_ratio < self.intrabar_min_rejection_wick_ratio
            ):
                return "Intrabar short trigger lacks strong upper-wick rejection."

        return _FibExecutionTrigger(
            candle=trigger,
            previous_candle=previous,
            volume_ratio=volume_ratio,
            body_ratio=body_ratio,
            close_position=close_position,
        )

    def _entry_block(
        self,
        *,
        direction: SignalDirection,
        candles: list[OHLCVCandle],
        latest: OHLCVCandle,
        fast_current: Decimal,
        fast_previous: Decimal,
        slow_current: Decimal,
        slow_previous: Decimal,
        atr: Decimal,
        volume_ratio: Decimal,
    ) -> _FibSetup | str:
        setup = self._entry_setup(
            direction=direction,
            candles=candles,
            latest=latest,
            fast_current=fast_current,
            fast_previous=fast_previous,
            slow_current=slow_current,
            slow_previous=slow_previous,
            atr=atr,
            volume_ratio=volume_ratio,
            require_parent_volume=True,
        )
        if not isinstance(setup, _FibSetup):
            return setup
        trigger = self._parent_rejection_trigger(
            direction=direction,
            setup=setup,
            latest=latest,
            previous=candles[-2],
            atr=atr,
        )
        return setup if trigger is None else trigger

    def _entry_signal(
        self,
        *,
        context: StrategyContext,
        setup: _FibSetup,
        atr: Decimal,
        metadata: dict[str, Any],
        reason: str,
        entry_candle: OHLCVCandle | None = None,
        entry_type: str = "fib_ma_pullback",
    ) -> StrategySignal:
        latest = entry_candle or context.latest_candle
        assert latest is not None

        stop_buffer = atr * self.stop_buffer_atr if atr > 0 else latest.close * self.fallback_stop_pct
        if setup.direction == SignalDirection.LONG:
            action = SignalAction.ENTER_LONG
            stop_loss = max(setup.swing_low - stop_buffer, Decimal("0.00000001"))
            risk_distance = max(latest.close - stop_loss, latest.close * self.fallback_stop_pct)
            fib_target = setup.swing_high + (setup.swing_range * self.take_profit_extension)
            rr_target = latest.close + (risk_distance * self.min_reward_r)
            take_profit = max(fib_target, rr_target)
        else:
            action = SignalAction.ENTER_SHORT
            stop_loss = setup.swing_high + stop_buffer
            risk_distance = max(stop_loss - latest.close, latest.close * self.fallback_stop_pct)
            fib_target = setup.swing_low - (setup.swing_range * self.take_profit_extension)
            rr_target = latest.close - (risk_distance * self.min_reward_r)
            take_profit = max(min(fib_target, rr_target), Decimal("0.00000001"))

        trend_gap_pct = (
            abs(metadata["ema_fast"] - metadata["ema_slow"]) / latest.close
            if latest.close > 0
            else Decimal("0")
        )
        volume_bonus = max(min(metadata["volume_ratio"] - Decimal("1"), Decimal("1")), Decimal("0"))
        confidence = Decimal("0.58") + min(trend_gap_pct * Decimal("10"), Decimal("0.12")) + (volume_bonus * Decimal("0.04"))

        return StrategySignal(
            strategy_name=self.name,
            pair=context.pair,
            interval=context.interval,
            action=action,
            confidence=clamp_confidence(confidence),
            reason=reason,
            timestamp_ms=latest.close_time_ms,
            direction=setup.direction,
            entry_price=latest.close,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata={
                **metadata,
                **setup.to_metadata(),
                "entry_type": entry_type,
                "fib_entry_zone": "0.382-0.618",
                "fib_min": self.fib_min,
                "fib_max": self.fib_max,
                "stop_model": "swing_invalidated_atr_buffer",
                "take_profit_model": "fib_extension_or_min_rr",
                "take_profit_extension": self.take_profit_extension,
                "min_reward_r": self.min_reward_r,
                "stop_atr_multiple": self.stop_buffer_atr,
            },
        )

    def _evaluate_exit(
        self,
        *,
        context: StrategyContext,
        open_position: dict[str, Any],
        fast_current: Decimal,
        slow_current: Decimal,
        latest: OHLCVCandle,
        metadata: dict[str, Any],
    ) -> StrategySignal | None:
        source_strategy = str(open_position.get("strategy_name") or "")
        if source_strategy and source_strategy != self.name:
            return self._hold(
                context,
                f"Open position is managed by {source_strategy}.",
                {**metadata, "entry_type": "fib_ma_pullback"},
            )

        direction = _direction(open_position.get("direction"))
        if direction is None:
            return self._hold(context, "Open position direction is unavailable.", metadata)

        if direction == SignalDirection.LONG and fast_current < slow_current:
            return StrategySignal(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                action=SignalAction.EXIT_LONG,
                confidence=Decimal("0.62"),
                reason="Fib MA exit: bullish moving-average structure flipped bearish.",
                timestamp_ms=latest.close_time_ms,
                direction=SignalDirection.LONG,
                entry_price=latest.close,
                metadata={
                    **metadata,
                    "entry_type": "fib_ma_exit",
                    "exit_trigger_type": "fib_ma_trend_flip",
                },
            )
        if direction == SignalDirection.SHORT and fast_current > slow_current:
            return StrategySignal(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                action=SignalAction.EXIT_SHORT,
                confidence=Decimal("0.62"),
                reason="Fib MA exit: bearish moving-average structure flipped bullish.",
                timestamp_ms=latest.close_time_ms,
                direction=SignalDirection.SHORT,
                entry_price=latest.close,
                metadata={
                    **metadata,
                    "entry_type": "fib_ma_exit",
                    "exit_trigger_type": "fib_ma_trend_flip",
                },
            )
        return None

    def _base_metadata(
        self,
        *,
        context: StrategyContext,
        fast_current: Decimal,
        fast_previous: Decimal,
        slow_current: Decimal,
        slow_previous: Decimal,
        atr_current: Decimal,
        volume_ratio: Decimal,
        raw_volume: Decimal,
        effective_volume: Decimal,
        average_volume: Decimal,
        volume_projection_factor: Decimal,
    ) -> dict[str, Any]:
        latest = context.latest_candle
        body_ratio = Decimal("0")
        if latest is not None and latest.high > latest.low:
            body_ratio = abs(latest.close - latest.open) / (latest.high - latest.low)
        return {
            "ema_fast": fast_current,
            "ema_fast_previous": fast_previous,
            "ema_slow": slow_current,
            "ema_slow_previous": slow_previous,
            "ema_fast_period": self.fast_period,
            "ema_slow_period": self.slow_period,
            "atr": atr_current,
            "atr_period": self.atr_period,
            "volume_ratio": volume_ratio,
            "raw_volume": raw_volume,
            "effective_volume": effective_volume,
            "volume_baseline": average_volume,
            "volume_projection_factor": volume_projection_factor,
            "volume_baseline_model": "trimmed_prior_average",
            "body_ratio": body_ratio,
            "extension_atr": Decimal("0"),
        }

    def _hold(
        self,
        context: StrategyContext,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> StrategySignal:
        latest = context.latest_candle
        return StrategySignal.hold(
            strategy_name=self.name,
            pair=context.pair,
            interval=context.interval,
            timestamp_ms=latest.close_time_ms if latest is not None else 0,
            reason=reason,
            metadata=metadata,
        )


@dataclass(frozen=True)
class _FibSetup:
    direction: SignalDirection
    swing_low: Decimal
    swing_high: Decimal
    swing_low_time_ms: int
    swing_high_time_ms: int
    swing_range: Decimal
    fib_382: Decimal
    fib_500: Decimal
    fib_618: Decimal
    zone_low: Decimal
    zone_high: Decimal
    pullback_age_candles: int
    swing_method: str
    swing_start_index: int
    swing_end_index: int

    def to_metadata(self) -> dict[str, Any]:
        return {
            "fib_direction": self.direction.value,
            "fib_swing_low": self.swing_low,
            "fib_swing_high": self.swing_high,
            "fib_swing_low_time_ms": self.swing_low_time_ms,
            "fib_swing_high_time_ms": self.swing_high_time_ms,
            "fib_swing_range": self.swing_range,
            "fib_382": self.fib_382,
            "fib_500": self.fib_500,
            "fib_618": self.fib_618,
            "fib_zone_low": self.zone_low,
            "fib_zone_high": self.zone_high,
            "pullback_age_candles": self.pullback_age_candles,
            "fib_swing_method": self.swing_method,
            "fib_swing_start_index": self.swing_start_index,
            "fib_swing_end_index": self.swing_end_index,
        }


@dataclass(frozen=True)
class _FibExecutionTrigger:
    candle: OHLCVCandle
    previous_candle: OHLCVCandle
    volume_ratio: Decimal
    body_ratio: Decimal
    close_position: Decimal

    def to_metadata(self) -> dict[str, Any]:
        return {
            "fib_intrabar_trigger": True,
            "fib_intrabar_trigger_time_ms": self.candle.close_time_ms,
            "fib_intrabar_previous_time_ms": self.previous_candle.close_time_ms,
            "execution_volume_ratio": self.volume_ratio,
            "execution_body_ratio": self.body_ratio,
            "execution_close_position": self.close_position,
            "volume_ratio": self.volume_ratio,
            "body_ratio": self.body_ratio,
        }


def _find_fib_setup(
    candles: list[OHLCVCandle],
    *,
    direction: SignalDirection,
    lookback: int,
    max_pullback_candles: int,
    pivot_left_bars: int,
    pivot_right_bars: int,
    fib_min: Decimal,
    fib_max: Decimal,
    min_swing_range: Decimal,
) -> _FibSetup | None:
    impulse = find_latest_impulse(
        candles,
        direction=direction,
        lookback=lookback,
        max_pullback_candles=max_pullback_candles,
        left_bars=pivot_left_bars,
        right_bars=pivot_right_bars,
        min_range=min_swing_range,
    )
    if impulse is None or impulse.range <= 0:
        return None

    swing_low = impulse.low
    swing_high = impulse.high
    swing_range = impulse.range
    if direction == SignalDirection.LONG:
        fib_a = swing_high - (swing_range * fib_min)
        fib_b = swing_high - (swing_range * fib_max)
    else:
        fib_a = swing_low + (swing_range * fib_min)
        fib_b = swing_low + (swing_range * fib_max)
    fib_500 = (
        swing_high - (swing_range * Decimal("0.5"))
        if direction == SignalDirection.LONG
        else swing_low + (swing_range * Decimal("0.5"))
    )
    zone_low = min(fib_a, fib_b)
    zone_high = max(fib_a, fib_b)

    return _FibSetup(
        direction=direction,
        swing_low=swing_low,
        swing_high=swing_high,
        swing_low_time_ms=(
            impulse.start_time_ms
            if direction == SignalDirection.LONG
            else impulse.end_time_ms
        ),
        swing_high_time_ms=(
            impulse.end_time_ms
            if direction == SignalDirection.LONG
            else impulse.start_time_ms
        ),
        swing_range=swing_range,
        fib_382=fib_a,
        fib_500=fib_500,
        fib_618=fib_b,
        zone_low=zone_low,
        zone_high=zone_high,
        pullback_age_candles=(len(candles) - 1 - impulse.end_index),
        swing_method="confirmed_pivot_zigzag",
        swing_start_index=impulse.start_index,
        swing_end_index=impulse.end_index,
    )


def _open_position(features: dict[str, Any], pair: str) -> dict[str, Any] | None:
    raw = features.get("open_position")
    if not isinstance(raw, dict) or raw.get("pair") != pair:
        return None
    return raw


def _direction(value: Any) -> SignalDirection | None:
    if value == SignalDirection.LONG or value == SignalDirection.LONG.value:
        return SignalDirection.LONG
    if value == SignalDirection.SHORT or value == SignalDirection.SHORT.value:
        return SignalDirection.SHORT
    return None


def _execution_candles(features: dict[str, Any]) -> list[OHLCVCandle]:
    raw = features.get("execution_candles")
    if not isinstance(raw, list):
        return []
    return [candle for candle in raw if isinstance(candle, OHLCVCandle)]


def _robust_average_volume(values: list[Decimal], period: int) -> Decimal | None:
    """Return an outlier-resistant recent volume baseline."""

    if period <= 0:
        return None
    window = [value for value in values[-period:] if value >= 0]
    if len(window) < period:
        return None

    trimmed = window
    if len(window) >= 10:
        trim = max(1, len(window) // 10)
        ordered = sorted(window)
        trimmed = ordered[trim:-trim] or ordered

    return sum(trimmed, Decimal("0")) / Decimal(len(trimmed))


def _effective_latest_volume(
    latest_volume: Decimal,
    features: dict[str, Any],
) -> tuple[Decimal, Decimal]:
    completion_ratio = _decimal_feature(features, "parent_completion_ratio", Decimal("1"))
    if completion_ratio <= 0 or completion_ratio >= 1:
        return latest_volume, Decimal("1")

    completion_ratio = max(completion_ratio, Decimal("0.05"))
    projection_factor = Decimal("1") / completion_ratio
    return latest_volume * projection_factor, projection_factor


def _decimal_feature(
    features: dict[str, Any],
    key: str,
    default: Decimal,
) -> Decimal:
    try:
        return Decimal(str(features.get(key, default)))
    except Exception:
        return default


def _combine_hold_reason(long_block: str, short_block: str) -> str:
    if long_block == short_block:
        return long_block
    return f"No Fib MA pullback setup. Long: {long_block} Short: {short_block}"
