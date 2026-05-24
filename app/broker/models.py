from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from app.risk.models import RiskDecision, convert_for_json
from app.strategies.base import SignalAction, SignalDirection, StrategySignal


class PaperOrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PaperOrderStatus(str, Enum):
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class PaperExecutionStatus(str, Enum):
    FILLED = "FILLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class PaperOrder:
    order_id: str
    pair: str
    side: PaperOrderSide
    action: SignalAction
    quantity: Decimal
    price: Decimal
    leverage: Decimal
    status: PaperOrderStatus
    created_at_ms: int
    filled_at_ms: int | None = None
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def notional(self) -> Decimal:
        return abs(self.quantity * self.price)

    def to_dict(self) -> dict[str, Any]:
        return convert_for_json(asdict(self))


@dataclass(frozen=True)
class PaperFill:
    fill_id: str
    order_id: str
    pair: str
    side: PaperOrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal
    timestamp_ms: int
    realized_pnl: Decimal = Decimal("0")
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def notional(self) -> Decimal:
        return abs(self.quantity * self.price)

    def to_dict(self) -> dict[str, Any]:
        return convert_for_json(asdict(self))


@dataclass(frozen=True)
class PaperPosition:
    pair: str
    direction: SignalDirection
    quantity: Decimal
    entry_price: Decimal
    leverage: Decimal
    opened_at_ms: int
    updated_at_ms: int
    strategy_name: str
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    quote_to_margin_rate: Decimal = Decimal("1")
    unit_contract_value: Decimal = Decimal("1")
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def notional(self) -> Decimal:
        return abs(
            self.quantity
            * self.entry_price
            * self.unit_contract_value
            * self.quote_to_margin_rate
        )

    def margin_notional(self, price: Decimal) -> Decimal:
        return abs(
            self.quantity
            * price
            * self.unit_contract_value
            * self.quote_to_margin_rate
        )

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        if self.direction == SignalDirection.LONG:
            return (
                self.quantity
                * (mark_price - self.entry_price)
                * self.unit_contract_value
                * self.quote_to_margin_rate
            )
        return (
            self.quantity
            * (self.entry_price - mark_price)
            * self.unit_contract_value
            * self.quote_to_margin_rate
        )

    def to_dict(self) -> dict[str, Any]:
        return convert_for_json(asdict(self))


@dataclass(frozen=True)
class PaperAccountSnapshot:
    starting_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    equity: Decimal
    open_position_count: int
    open_notional: Decimal
    funding_paid: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return convert_for_json(asdict(self))


@dataclass(frozen=True)
class PaperExecutionReport:
    accepted: bool
    status: PaperExecutionStatus
    reason: str
    account: PaperAccountSnapshot
    order: PaperOrder | None = None
    fill: PaperFill | None = None
    position: PaperPosition | None = None
    risk_decision: RiskDecision | None = None
    signal: StrategySignal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.risk_decision is not None:
            data["risk_decision"] = self.risk_decision.to_dict()
        if self.signal is not None:
            data["signal"] = self.signal.to_dict()
        return convert_for_json(data)
