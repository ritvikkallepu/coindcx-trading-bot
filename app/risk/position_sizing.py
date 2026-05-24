from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.risk.models import InstrumentMetadata
from app.strategies.base import StrategySignal


@dataclass(frozen=True)
class PositionSizingResult:
    position_size: Decimal
    notional: Decimal
    max_loss: Decimal
    risk_budget: Decimal
    capped_by_leverage: bool = False


def fixed_fraction_position_size(
    *,
    account_equity: Decimal,
    max_risk_per_trade_pct: Decimal,
    signal: StrategySignal,
    leverage: Decimal,
    instrument: InstrumentMetadata | None = None,
    quote_to_margin_rate: Decimal = Decimal("1"),
    unit_contract_value: Decimal = Decimal("1"),
) -> PositionSizingResult:
    if account_equity <= 0:
        raise ValueError("account_equity must be positive.")
    if max_risk_per_trade_pct < 0:
        raise ValueError("max_risk_per_trade_pct cannot be negative.")
    if leverage <= 0:
        raise ValueError("leverage must be positive.")
    if quote_to_margin_rate <= 0:
        raise ValueError("quote_to_margin_rate must be positive.")
    if unit_contract_value <= 0:
        raise ValueError("unit_contract_value must be positive.")

    if max_risk_per_trade_pct == 0:
        return PositionSizingResult(
            position_size=Decimal("0"),
            notional=Decimal("0"),
            max_loss=Decimal("0"),
            risk_budget=Decimal("0"),
        )
    if signal.entry_price is None or signal.entry_price <= 0:
        raise ValueError("entry_price must be positive.")
    if signal.stop_loss is None or signal.stop_loss <= 0:
        raise ValueError("stop_loss must be positive.")

    entry_price = signal.entry_price
    stop_distance = abs(entry_price - signal.stop_loss)
    if stop_distance <= 0:
        raise ValueError("stop_loss must differ from entry_price.")

    risk_budget = account_equity * (max_risk_per_trade_pct / Decimal("100"))
    price_value_multiplier = quote_to_margin_rate * unit_contract_value
    stop_value_per_unit = stop_distance * price_value_multiplier
    raw_position_size = risk_budget / stop_value_per_unit
    raw_notional = raw_position_size * entry_price * price_value_multiplier
    max_notional = account_equity * leverage
    capped_by_leverage = raw_notional > max_notional

    if capped_by_leverage:
        position_size = max_notional / (entry_price * price_value_multiplier)
    else:
        position_size = raw_position_size

    if instrument is not None and instrument.quantity_step is not None:
        position_size = round_down_to_step(position_size, instrument.quantity_step)

    notional = position_size * entry_price * price_value_multiplier
    max_loss = position_size * stop_distance * price_value_multiplier

    return PositionSizingResult(
        position_size=position_size,
        notional=notional,
        max_loss=max_loss,
        risk_budget=risk_budget,
        capped_by_leverage=capped_by_leverage,
    )


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("quantity_step must be positive.")
    if value <= 0:
        return Decimal("0")
    return (value // step) * step
