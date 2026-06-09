"""Pure helpers for reconciling closed live futures positions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Iterable, Mapping

from app.strategies.base import SignalDirection


@dataclass(frozen=True)
class ClosedPositionAccounting:
    """A single closed-position accounting result in margin currency."""

    direction: SignalDirection
    exit_order_ids: tuple[str, ...]
    exit_price: Decimal
    quantity: Decimal
    gross_pnl: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    net_pnl: Decimal
    source: str


def normalize_position_direction(position: Mapping[str, Any]) -> SignalDirection:
    """Return a canonical direction from enum, text, or signed active quantity."""

    raw = position.get("direction")
    if isinstance(raw, SignalDirection):
        return raw
    if raw not in (None, ""):
        normalized = str(raw).strip().lower()
        if normalized == SignalDirection.LONG.value:
            return SignalDirection.LONG
        if normalized == SignalDirection.SHORT.value:
            return SignalDirection.SHORT

    active_pos = _decimal(position.get("active_pos"))
    if active_pos > 0:
        return SignalDirection.LONG
    if active_pos < 0:
        return SignalDirection.SHORT
    raise ValueError("Cannot determine closed position direction.")


def expected_exit_side(direction: SignalDirection) -> str:
    """Return the exchange order side that closes the supplied direction."""

    return "sell" if direction == SignalDirection.LONG else "buy"


def select_exit_orders(
    orders: Iterable[Mapping[str, Any]],
    *,
    pair: str,
    direction: SignalDirection,
) -> list[Mapping[str, Any]]:
    """Select the newest closing order, including exchange-split group members."""

    expected_side = expected_exit_side(direction)
    candidates = [
        order
        for order in orders
        if str(order.get("pair") or "").strip().upper() == pair.strip().upper()
        and str(order.get("side") or "").strip().lower() == expected_side
        and str(order.get("status") or "filled").strip().lower()
        in {"filled", "partially_cancelled", "partially_canceled"}
        and _positive_decimal(order.get("avg_price")) is not None
    ]
    candidates.sort(key=_order_updated_at, reverse=True)
    if not candidates:
        raise ValueError(
            f"No filled {expected_side} exit order found for {pair} "
            f"{direction.value} position."
        )

    latest = candidates[0]
    group_id = _meaningful_id(latest.get("group_id"))
    if group_id is None:
        return [latest]
    grouped = [
        order
        for order in candidates
        if _meaningful_id(order.get("group_id")) == group_id
    ]
    return grouped or [latest]


def order_fee_in_margin_currency(
    order: Mapping[str, Any],
    *,
    quote_to_margin_rate: Decimal,
    margin_currency: str,
) -> Decimal:
    """Convert an order fee into the configured margin currency."""

    fee = abs(_decimal(_order_value(order, "fee_amount")))
    if fee == 0 or margin_currency.strip().upper() != "INR":
        return fee
    conversion = _positive_decimal(
        _order_value(order, "settlement_currency_conversion_price")
    )
    return fee * (conversion or quote_to_margin_rate)


def total_order_fees_in_margin_currency(
    orders: Iterable[Mapping[str, Any]],
    *,
    quote_to_margin_rate: Decimal,
    margin_currency: str,
) -> Decimal:
    """Return the sum of order fees converted to margin currency."""

    return sum(
        (
            order_fee_in_margin_currency(
                order,
                quote_to_margin_rate=quote_to_margin_rate,
                margin_currency=margin_currency,
            )
            for order in orders
        ),
        Decimal("0"),
    )


def matching_transaction_totals(
    transactions: Iterable[Mapping[str, Any]],
    *,
    pair: str,
    exit_order_ids: Iterable[str],
) -> tuple[Decimal, Decimal, int]:
    """Return exact exchange PnL and fees for matching close-order transactions."""

    ids = {str(order_id) for order_id in exit_order_ids if str(order_id)}
    gross = Decimal("0")
    fees = Decimal("0")
    matched = 0
    seen: set[str] = set()
    for transaction in transactions:
        if str(transaction.get("pair") or "").strip().upper() != pair.strip().upper():
            continue
        if str(transaction.get("parent_id") or "") not in ids:
            continue
        identity = transaction_identity(transaction)
        if identity in seen:
            continue
        seen.add(identity)
        gross += _decimal(transaction.get("amount"))
        fees += abs(_decimal(transaction.get("fee_amount")))
        matched += 1
    return gross, fees, matched


def transaction_net_amount(transaction: Mapping[str, Any]) -> Decimal:
    """Return the margin-currency cash impact of one exchange transaction."""

    return _decimal(transaction.get("amount")) - abs(_decimal(transaction.get("fee_amount")))


def transaction_identity(transaction: Mapping[str, Any]) -> str:
    """Return a stable identity even when the exchange omits a transaction id."""

    explicit = _meaningful_id(
        transaction.get("id")
        or transaction.get("transaction_id")
        or transaction.get("trade_id")
    )
    if explicit is not None:
        return explicit
    stable_fields = (
        "pair",
        "stage",
        "parent_id",
        "parent_type",
        "position_id",
        "amount",
        "fee_amount",
        "settlement_amount",
        "created_at",
        "updated_at",
    )
    payload = "|".join(str(transaction.get(key) or "") for key in stable_fields)
    return f"derived:{sha256(payload.encode('utf-8')).hexdigest()}"


def calculate_closed_position_accounting(
    *,
    position: Mapping[str, Any],
    exit_orders: Iterable[Mapping[str, Any]],
    quote_to_margin_rate: Decimal,
    unit_contract_value: Decimal,
    margin_currency: str,
    entry_fee: Decimal = Decimal("0"),
    transaction_gross_pnl: Decimal | None = None,
    transaction_exit_fee: Decimal | None = None,
) -> ClosedPositionAccounting:
    """Calculate a closed position result, preferring exact transaction totals."""

    direction = normalize_position_direction(position)
    selected_orders = select_exit_orders(
        exit_orders,
        pair=str(position.get("pair") or ""),
        direction=direction,
    )
    entry_price = _required_positive_decimal(position.get("avg_price"), "entry price")
    quantity = abs(_decimal(position.get("active_pos")))
    if quantity <= 0:
        raise ValueError("Closed position quantity must be positive.")

    exit_price = _weighted_exit_price(selected_orders)
    exit_order_ids = tuple(
        order_id
        for order in selected_orders
        if (order_id := _meaningful_id(order.get("id") or order.get("order_id")))
    )
    if not exit_order_ids:
        raise ValueError("Closing order is missing an order id.")

    if transaction_gross_pnl is not None and transaction_exit_fee is not None:
        gross_pnl = transaction_gross_pnl
        exit_fee = abs(transaction_exit_fee)
        source = "exchange_transactions"
    else:
        side_multiplier = (
            Decimal("1") if direction == SignalDirection.LONG else Decimal("-1")
        )
        gross_pnl = (
            (exit_price - entry_price)
            * quantity
            * side_multiplier
            * unit_contract_value
            * quote_to_margin_rate
        )
        exit_fee = total_order_fees_in_margin_currency(
            selected_orders,
            quote_to_margin_rate=quote_to_margin_rate,
            margin_currency=margin_currency,
        )
        source = "deterministic_fallback"

    normalized_entry_fee = abs(entry_fee)
    return ClosedPositionAccounting(
        direction=direction,
        exit_order_ids=exit_order_ids,
        exit_price=exit_price,
        quantity=quantity,
        gross_pnl=gross_pnl,
        entry_fee=normalized_entry_fee,
        exit_fee=exit_fee,
        net_pnl=gross_pnl - normalized_entry_fee - exit_fee,
        source=source,
    )


def _weighted_exit_price(orders: Iterable[Mapping[str, Any]]) -> Decimal:
    weighted_total = Decimal("0")
    filled_total = Decimal("0")
    for order in orders:
        price = _required_positive_decimal(order.get("avg_price"), "exit price")
        filled = _filled_quantity(order)
        if filled <= 0:
            continue
        weighted_total += price * filled
        filled_total += filled
    if filled_total <= 0:
        raise ValueError("Closing orders have no filled quantity.")
    return weighted_total / filled_total


def _filled_quantity(order: Mapping[str, Any]) -> Decimal:
    total = abs(_decimal(order.get("total_quantity")))
    remaining = abs(_decimal(order.get("remaining_quantity")))
    cancelled = abs(
        _decimal(order.get("cancelled_quantity") or order.get("canceled_quantity"))
    )
    filled = total - remaining - cancelled
    return filled if filled > 0 else total


def _order_updated_at(order: Mapping[str, Any]) -> int:
    try:
        return int(order.get("updated_at") or order.get("created_at") or 0)
    except (TypeError, ValueError):
        return 0


def _required_positive_decimal(value: Any, label: str) -> Decimal:
    result = _positive_decimal(value)
    if result is None:
        raise ValueError(f"Closed position {label} must be positive.")
    return result


def _positive_decimal(value: Any) -> Decimal | None:
    result = _decimal(value)
    return result if result > 0 else None


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid decimal value: {value!r}") from exc


def _meaningful_id(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return None if normalized.lower() in {"", "none", "null"} else normalized


def _order_value(order: Mapping[str, Any], key: str) -> Any:
    value = order.get(key)
    if value not in (None, ""):
        return value
    raw = order.get("raw")
    return raw.get(key) if isinstance(raw, Mapping) else None
