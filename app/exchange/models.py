from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


ORDER_SIDES = {"buy", "sell"}
ORDER_TYPES = {
    "market_order",
    "limit_order",
    "stop_limit",
    "stop_market",
    "take_profit_limit",
    "take_profit_market",
}
TIME_IN_FORCE_OPTIONS = {
    "good_till_cancel",
    "immediate_or_cancel",
    "fill_or_kill",
}
MARGIN_TYPES = {"isolated", "crossed"}


def _as_api_number(value: Decimal | int | float | str | None) -> int | float | str | None:
    if isinstance(value, Decimal):
        normalized = value.normalize()
        if normalized == normalized.to_integral():
            return int(normalized)
        return float(normalized)
    return value


@dataclass(frozen=True)
class FuturesOrderRequest:
    side: str
    pair: str
    order_type: str
    total_quantity: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None
    leverage: int | None = None
    notification: str = "no_notification"
    time_in_force: str | None = "good_till_cancel"
    hidden: bool = False
    post_only: bool = False
    margin_currency_short_name: str = "USDT"
    position_margin_type: str | None = None
    take_profit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None

    def __post_init__(self) -> None:
        side = self.side.lower()
        order_type = self.order_type.lower()
        if side not in ORDER_SIDES:
            raise ValueError(f"Invalid futures order side: {self.side}")
        if order_type not in ORDER_TYPES:
            raise ValueError(f"Invalid futures order type: {self.order_type}")
        if self.total_quantity <= 0:
            raise ValueError("total_quantity must be positive.")
        if order_type == "market_order" and self.time_in_force is not None:
            raise ValueError("CoinDCX says time_in_force should not be sent for market orders.")
        if order_type in {"limit_order", "stop_limit", "take_profit_limit"}:
            if self.price is None:
                raise ValueError("price is required for limit-style futures orders.")
        if order_type in {
            "stop_limit",
            "stop_market",
            "take_profit_limit",
            "take_profit_market",
        }:
            if self.stop_price is None:
                raise ValueError("stop_price is required for stop/take-profit futures orders.")
        if self.time_in_force is not None and self.time_in_force not in TIME_IN_FORCE_OPTIONS:
            raise ValueError(f"Invalid time_in_force: {self.time_in_force}")
        if self.position_margin_type is not None and self.position_margin_type not in MARGIN_TYPES:
            raise ValueError(f"Invalid position_margin_type: {self.position_margin_type}")

    def to_api_order(self) -> dict[str, Any]:
        order: dict[str, Any] = {
            "side": self.side.lower(),
            "pair": self.pair,
            "order_type": self.order_type.lower(),
            "price": _as_api_number(self.price),
            "stop_price": _as_api_number(self.stop_price),
            "total_quantity": _as_api_number(self.total_quantity),
            "notification": self.notification,
            "hidden": self.hidden,
            "post_only": self.post_only,
            "margin_currency_short_name": self.margin_currency_short_name.upper(),
        }

        if self.leverage is not None:
            order["leverage"] = self.leverage
        if self.time_in_force is not None:
            order["time_in_force"] = self.time_in_force
        if self.position_margin_type is not None:
            order["position_margin_type"] = self.position_margin_type
        if self.take_profit_price is not None:
            order["take_profit_price"] = _as_api_number(self.take_profit_price)
        if self.stop_loss_price is not None:
            order["stop_loss_price"] = _as_api_number(self.stop_loss_price)

        return {key: value for key, value in order.items() if value is not None}
