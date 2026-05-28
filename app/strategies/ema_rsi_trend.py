from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.data.indicators import (
    average_true_range,
    exponential_moving_average,
    relative_strength_index,
)
from app.strategies.base import (
    SignalAction,
    SignalDirection,
    Strategy,
    StrategyContext,
    StrategySignal,
    clamp_confidence,
)


@dataclass(frozen=True)
class EMARSICrossoverStrategy(Strategy):
    name: str = "ema_rsi_trend"
    fast_period: int = 9
    slow_period: int = 21
    rsi_period: int = 14
    long_rsi_min: Decimal = Decimal("65")
    long_rsi_max: Decimal = Decimal("72")
    short_rsi_min: Decimal = Decimal("20")
    short_rsi_max: Decimal = Decimal("50")
    atr_period: int = 14
    stop_atr_multiple: Decimal = Decimal("1.5")
    take_profit_atr_multiple: Decimal = Decimal("3")
    atr_trailing_multiple: Decimal = Decimal("1.2")
    fallback_stop_pct: Decimal = Decimal("0.01")
    fallback_take_profit_pct: Decimal = Decimal("0.02")

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        latest = context.latest_candle
        if latest is None:
            return self._hold(context, "No candles available.")

        required = max(self.slow_period, self.rsi_period + 1, self.atr_period) + 1
        if len(context.candles) < required:
            return self._hold(context, f"Need at least {required} candles.")

        closes = context.candles.closes()
        fast = exponential_moving_average(closes, self.fast_period)
        slow = exponential_moving_average(closes, self.slow_period)
        rsi_values = relative_strength_index(closes, self.rsi_period)
        atr_values = average_true_range(context.candles, self.atr_period)

        fast_previous, fast_current = fast[-2], fast[-1]
        slow_previous, slow_current = slow[-2], slow[-1]
        rsi_current = rsi_values[-1]
        atr_current = atr_values[-1]

        metadata = {
            "ema_fast": fast_current,
            "ema_slow": slow_current,
            "ema_fast_previous": fast_previous,
            "ema_slow_previous": slow_previous,
            "rsi": rsi_current,
            "atr": atr_current,
        }

        if None in {fast_previous, fast_current, slow_previous, slow_current, rsi_current}:
            return self._hold(context, "Indicators are still warming up.", metadata)

        assert fast_previous is not None
        assert fast_current is not None
        assert slow_previous is not None
        assert slow_current is not None
        assert rsi_current is not None

        crossed_up = fast_previous <= slow_previous and fast_current > slow_current
        crossed_down = fast_previous >= slow_previous and fast_current < slow_current

        if crossed_up and self.long_rsi_min <= rsi_current <= self.long_rsi_max:
            return self._entry_signal(
                context=context,
                action=SignalAction.ENTER_LONG,
                direction=SignalDirection.LONG,
                reason="Fast EMA crossed above slow EMA with RSI confirming bullish momentum.",
                atr=atr_current,
                metadata=metadata,
            )

        if crossed_down and self.short_rsi_min <= rsi_current <= self.short_rsi_max:
            return self._entry_signal(
                context=context,
                action=SignalAction.ENTER_SHORT,
                direction=SignalDirection.SHORT,
                reason="Fast EMA crossed below slow EMA with RSI confirming bearish momentum.",
                atr=atr_current,
                metadata=metadata,
            )

        if crossed_down:
            return StrategySignal(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                action=SignalAction.EXIT_LONG,
                confidence=Decimal("0.55"),
                reason="Fast EMA crossed below slow EMA; long trend invalidated.",
                timestamp_ms=latest.close_time_ms,
                direction=SignalDirection.LONG,
                entry_price=latest.close,
                metadata=metadata,
            )

        if crossed_up:
            return StrategySignal(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                action=SignalAction.EXIT_SHORT,
                confidence=Decimal("0.55"),
                reason="Fast EMA crossed above slow EMA; short trend invalidated.",
                timestamp_ms=latest.close_time_ms,
                direction=SignalDirection.SHORT,
                entry_price=latest.close,
                metadata=metadata,
            )

        return self._hold(context, "No EMA crossover signal.", metadata)

    def _entry_signal(
        self,
        *,
        context: StrategyContext,
        action: SignalAction,
        direction: SignalDirection,
        reason: str,
        atr: Decimal | None,
        metadata: dict,
    ) -> StrategySignal:
        latest = context.latest_candle
        assert latest is not None

        stop_distance = (
            atr * self.stop_atr_multiple
            if atr is not None and atr > 0
            else latest.close * self.fallback_stop_pct
        )
        take_profit_distance = (
            atr * self.take_profit_atr_multiple
            if atr is not None and atr > 0
            else latest.close * self.fallback_take_profit_pct
        )

        if direction == SignalDirection.LONG:
            stop_loss = latest.close - stop_distance
            take_profit = latest.close + take_profit_distance
        else:
            stop_loss = latest.close + stop_distance
            take_profit = latest.close - take_profit_distance

        rsi_value = metadata.get("rsi") or Decimal("50")
        confidence = Decimal("0.55") + (abs(rsi_value - Decimal("50")) / Decimal("100"))

        metadata = {
            "stop_atr_multiple": self.stop_atr_multiple,
            "take_profit_atr_multiple": self.take_profit_atr_multiple,
            "atr_trailing_multiple": self.atr_trailing_multiple,
            "trailing_atr_multiple": self.atr_trailing_multiple,
            **metadata,
        }

        return StrategySignal(
            strategy_name=self.name,
            pair=context.pair,
            interval=context.interval,
            action=action,
            confidence=clamp_confidence(confidence),
            reason=reason,
            timestamp_ms=latest.close_time_ms,
            direction=direction,
            entry_price=latest.close,
            stop_loss=max(stop_loss, Decimal("0")),
            take_profit=max(take_profit, Decimal("0")),
            metadata=metadata,
        )

    def _hold(
        self,
        context: StrategyContext,
        reason: str,
        metadata: dict | None = None,
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
