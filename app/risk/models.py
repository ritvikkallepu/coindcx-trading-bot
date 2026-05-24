from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence

from app.strategies.base import SignalDirection, StrategySignal


class RiskDecisionStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class InstrumentMetadata:
    pair: str
    quantity_step: Decimal | None = None
    min_quantity: Decimal | None = None
    min_notional: Decimal | None = None
    max_leverage: Decimal | None = None
    tick_size: Decimal | None = None
    margin_currency: str = "INR"
    quote_currency: str = "USDT"
    settle_currency: str | None = None
    unit_contract_value: Decimal = Decimal("1")
    quote_to_margin_rate: Decimal | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        pair: str,
        values: Mapping[str, Any],
    ) -> InstrumentMetadata:
        values = unwrap_instrument_mapping(values)
        return cls(
            pair=pair,
            quantity_step=first_decimal(
                values,
                "quantity_step",
                "quantity_increment",
                "step_size",
                "qty_step",
            ),
            min_quantity=first_decimal(
                values,
                "min_quantity",
                "minimum_quantity",
                "min_qty",
                "min_trade_size",
            ),
            min_notional=first_decimal(
                values,
                "min_notional",
                "minimum_notional",
                "min_order_value",
            ),
            max_leverage=instrument_max_leverage(values),
            tick_size=first_decimal(values, "tick_size", "price_increment"),
            margin_currency=str(
                values.get("margin_currency_short_name")
                or values.get("margin_currency")
                or "INR"
            ).upper(),
            quote_currency=str(
                values.get("quote_currency_short_name")
                or values.get("quote_currency")
                or "USDT"
            ).upper(),
            settle_currency=(
                str(
                    values.get("settle_currency_short_name")
                    or values.get("settle_currency")
                ).upper()
                if values.get("settle_currency_short_name")
                or values.get("settle_currency")
                else None
            ),
            unit_contract_value=first_decimal(
                values,
                "unit_contract_value",
                "contract_value",
            )
            or Decimal("1"),
            quote_to_margin_rate=first_decimal(
                values,
                "quote_to_margin_rate",
            ),
            raw=dict(values),
        )


@dataclass(frozen=True)
class OpenPosition:
    pair: str
    direction: SignalDirection | str
    quantity: Decimal
    entry_price: Decimal
    leverage: Decimal = Decimal("1")
    stop_loss: Decimal | None = None
    quote_to_margin_rate: Decimal = Decimal("1")
    unit_contract_value: Decimal = Decimal("1")

    @property
    def is_open(self) -> bool:
        return self.quantity != 0

    @property
    def notional(self) -> Decimal:
        return abs(
            self.quantity
            * self.entry_price
            * self.unit_contract_value
            * self.quote_to_margin_rate
        )

    @property
    def loss_at_stop(self) -> Decimal | None:
        if self.stop_loss is None:
            return None
        return (
            abs(self.entry_price - self.stop_loss)
            * abs(self.quantity)
            * self.unit_contract_value
            * self.quote_to_margin_rate
        )


OpenPositions = Sequence[OpenPosition] | int


@dataclass(frozen=True)
class RiskContext:
    signal: StrategySignal
    account_equity: Decimal
    available_equity: Decimal | None = None
    risk_base_mode: str = "current"
    open_positions: OpenPositions = ()
    daily_realized_pnl: Decimal = Decimal("0")
    daily_loss_limit_equity: Decimal | None = None
    instrument: InstrumentMetadata | None = None
    requested_leverage: Decimal | None = None
    quote_to_margin_rate: Decimal = Decimal("1")
    unit_contract_value: Decimal = Decimal("1")
    trading_mode: str = "paper"
    live_trading_enabled: bool = False

    @property
    def live_trading_allowed(self) -> bool:
        return self.trading_mode.lower() == "live" and self.live_trading_enabled

    @property
    def open_position_count(self) -> int:
        if isinstance(self.open_positions, int):
            return max(self.open_positions, 0)
        return sum(1 for position in self.open_positions if position.is_open)


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    signal: StrategySignal
    position_size: Decimal | None = None
    notional: Decimal | None = None
    leverage: Decimal | None = None
    max_loss: Decimal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> RiskDecisionStatus:
        if self.approved:
            return RiskDecisionStatus.APPROVED
        return RiskDecisionStatus.REJECTED

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return convert_for_json(data)


def first_decimal(values: Mapping[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        if key in values:
            return decimal_or_none(values[key])
    return None


def unwrap_instrument_mapping(values: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = values.get("instrument")
    if isinstance(nested, Mapping):
        return nested
    return values


def instrument_max_leverage(values: Mapping[str, Any]) -> Decimal | None:
    direct = first_decimal(values, "max_leverage", "leverage")
    long_limit = first_decimal(values, "max_leverage_long")
    short_limit = first_decimal(values, "max_leverage_short")
    directional_limits = [
        limit for limit in (long_limit, short_limit) if limit is not None
    ]
    if direct is not None and directional_limits:
        return min([direct, *directional_limits])
    if directional_limits:
        return min(directional_limits)
    return direct


def convert_for_json(value: Any) -> Any:
    if isinstance(value, StrategySignal):
        return value.to_dict()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: convert_for_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [convert_for_json(item) for item in value]
    if isinstance(value, tuple):
        return [convert_for_json(item) for item in value]
    return value
