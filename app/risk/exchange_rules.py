from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from app.risk.models import InstrumentMetadata
from app.strategies.base import SignalDirection, StrategySignal


def normalize_signal_prices_to_tick(
    signal: StrategySignal,
    instrument: InstrumentMetadata | None,
) -> StrategySignal:
    if instrument is None or instrument.tick_size is None:
        return signal
    if signal.direction is None:
        return signal

    tick_size = instrument.tick_size
    entry_price = signal.entry_price
    stop_loss = signal.stop_loss
    take_profit = signal.take_profit
    adjusted: dict[str, Decimal] = {}
    original: dict[str, Decimal] = {}

    if signal.direction == SignalDirection.LONG:
        entry_price = round_price(signal.entry_price, tick_size, "up")
        stop_loss = round_price(signal.stop_loss, tick_size, "down")
        take_profit = round_price(signal.take_profit, tick_size, "up")
    elif signal.direction == SignalDirection.SHORT:
        entry_price = round_price(signal.entry_price, tick_size, "down")
        stop_loss = round_price(signal.stop_loss, tick_size, "up")
        take_profit = round_price(signal.take_profit, tick_size, "down")

    for field_name, before, after in (
        ("entry_price", signal.entry_price, entry_price),
        ("stop_loss", signal.stop_loss, stop_loss),
        ("take_profit", signal.take_profit, take_profit),
    ):
        if before is not None and after is not None and before != after:
            original[field_name] = before
            adjusted[field_name] = after

    if not adjusted:
        return signal

    return replace(
        signal,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        metadata={
            **signal.metadata,
            "price_tick_normalized": True,
            "price_tick_size": tick_size,
            "price_tick_original": original,
            "price_tick_adjusted": adjusted,
        },
    )


def liquidation_guard(
    *,
    signal: StrategySignal,
    leverage: Decimal,
    buffer_pct: Decimal,
) -> tuple[str | None, dict[str, Decimal]]:
    if buffer_pct < 0:
        return "Liquidation buffer percentage cannot be negative.", {}
    if leverage <= 0:
        return "Requested leverage must be positive.", {}
    if signal.direction is None or signal.entry_price is None or signal.stop_loss is None:
        return None, {}

    entry_price = signal.entry_price
    liquidation_distance = entry_price / leverage
    if signal.direction == SignalDirection.LONG:
        liquidation_price = max(Decimal("0"), entry_price - liquidation_distance)
        minimum_stop = liquidation_price + _percent_of(entry_price, buffer_pct)
        metadata = {
            "estimated_liquidation_price": liquidation_price,
            "liquidation_buffer_pct": buffer_pct,
            "minimum_stop_above_liquidation": minimum_stop,
        }
        if signal.stop_loss <= minimum_stop:
            return (
                "Stop loss is too close to estimated liquidation price: "
                f"{signal.stop_loss} <= {minimum_stop}.",
                metadata,
            )
        return None, metadata

    liquidation_price = entry_price + liquidation_distance
    maximum_stop = liquidation_price - _percent_of(entry_price, buffer_pct)
    metadata = {
        "estimated_liquidation_price": liquidation_price,
        "liquidation_buffer_pct": buffer_pct,
        "maximum_stop_below_liquidation": maximum_stop,
    }
    if signal.stop_loss >= maximum_stop:
        return (
            "Stop loss is too close to estimated liquidation price: "
            f"{signal.stop_loss} >= {maximum_stop}.",
            metadata,
        )
    return None, metadata


def round_price(
    value: Decimal | None,
    tick_size: Decimal,
    mode: str,
) -> Decimal | None:
    if value is None:
        return None
    if tick_size <= 0:
        raise ValueError("tick_size must be positive.")
    if value <= 0:
        return value

    rounded_down = (value // tick_size) * tick_size
    if mode == "down" or rounded_down == value:
        return rounded_down
    if mode == "up":
        return rounded_down + tick_size
    raise ValueError(f"Unsupported tick rounding mode: {mode}")


def _percent_of(value: Decimal, pct: Decimal) -> Decimal:
    return value * (pct / Decimal("100"))
