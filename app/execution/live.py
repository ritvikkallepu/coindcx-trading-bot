from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.exchange.coindcx_rest import CoinDCXFuturesClient
from app.exchange.errors import CoinDCXAPIError
from app.exchange.models import FuturesOrderRequest
from app.risk.limits import is_entry_signal, is_exit_signal
from app.risk.models import RiskDecision, convert_for_json
from app.strategies.base import SignalAction, SignalDirection, StrategySignal


logger = logging.getLogger(__name__)


ORDER_SYNC_STATUSES = (
    "open,filled,partially_filled,partially_cancelled,cancelled,rejected,untriggered"
)


@dataclass(frozen=True)
class LiveOrderSnapshot:
    order_id: str
    pair: str
    side: str
    status: str
    order_type: str
    total_quantity: Decimal
    remaining_quantity: Decimal
    avg_price: Decimal | None
    fee_amount: Decimal
    updated_at: int | None
    raw: dict[str, Any]

    @property
    def filled_quantity(self) -> Decimal:
        return max(Decimal("0"), self.total_quantity - self.remaining_quantity)

    @property
    def is_terminal(self) -> bool:
        return self.status in {"filled", "cancelled", "canceled", "rejected"}

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> LiveOrderSnapshot:
        return cls(
            order_id=str(values.get("id") or ""),
            pair=str(values.get("pair") or ""),
            side=str(values.get("side") or "").lower(),
            status=str(values.get("status") or "").lower(),
            order_type=str(values.get("order_type") or "").lower(),
            total_quantity=_decimal_or_zero(values.get("total_quantity")),
            remaining_quantity=_decimal_or_zero(values.get("remaining_quantity")),
            avg_price=_positive_decimal_or_none(values.get("avg_price")),
            fee_amount=_decimal_or_zero(values.get("fee_amount")),
            updated_at=_int_or_none(values.get("updated_at")),
            raw=dict(values),
        )

    def to_dict(self) -> dict[str, Any]:
        return convert_for_json(asdict(self))


@dataclass(frozen=True)
class LivePositionSnapshot:
    position_id: str
    pair: str
    active_pos: Decimal
    quantity: Decimal
    direction: SignalDirection | None
    avg_price: Decimal | None
    liquidation_price: Decimal | None
    locked_margin: Decimal
    locked_order_margin: Decimal
    take_profit_trigger: Decimal | None
    stop_loss_trigger: Decimal | None
    leverage: Decimal | None
    margin_type: str
    margin_currency_short_name: str
    settlement_currency_avg_price: Decimal | None
    updated_at: int | None
    raw: dict[str, Any]

    @property
    def is_open(self) -> bool:
        return self.quantity > 0

    @property
    def has_stop_loss(self) -> bool:
        return self.stop_loss_trigger is not None and self.stop_loss_trigger > 0

    @property
    def has_take_profit(self) -> bool:
        return self.take_profit_trigger is not None and self.take_profit_trigger > 0

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> LivePositionSnapshot:
        active_pos = _decimal_or_zero(values.get("active_pos"))
        direction = None
        if active_pos > 0:
            direction = SignalDirection.LONG
        elif active_pos < 0:
            direction = SignalDirection.SHORT
        return cls(
            position_id=str(values.get("id") or ""),
            pair=str(values.get("pair") or ""),
            active_pos=active_pos,
            quantity=abs(active_pos),
            direction=direction,
            avg_price=_positive_decimal_or_none(values.get("avg_price")),
            liquidation_price=_positive_decimal_or_none(values.get("liquidation_price")),
            locked_margin=_decimal_or_zero(values.get("locked_margin")),
            locked_order_margin=_decimal_or_zero(values.get("locked_order_margin")),
            take_profit_trigger=_positive_decimal_or_none(
                values.get("take_profit_trigger")
            ),
            stop_loss_trigger=_positive_decimal_or_none(values.get("stop_loss_trigger")),
            leverage=_positive_decimal_or_none(values.get("leverage")),
            margin_type=str(values.get("margin_type") or "isolated").lower(),
            margin_currency_short_name=str(
                values.get("margin_currency_short_name") or ""
            ).upper(),
            settlement_currency_avg_price=_positive_decimal_or_none(
                values.get("settlement_currency_avg_price")
            ),
            updated_at=_int_or_none(values.get("updated_at")),
            raw=dict(values),
        )

    def to_dict(self) -> dict[str, Any]:
        return convert_for_json(asdict(self))


@dataclass(frozen=True)
class LiveSyncResult:
    ok: bool
    reason: str
    orders: list[LiveOrderSnapshot]
    position: LivePositionSnapshot | None = None
    tpsl_response: Any = None
    exit_response: Any = None
    cancel_response: Any = None
    safety_failure: bool = False
    submitted_to_exchange: bool = False

    def to_dict(self) -> dict[str, Any]:
        return convert_for_json(asdict(self))


@dataclass(frozen=True)
class LiveExecutionReport:
    accepted: bool
    dry_run: bool
    reason: str
    order_request: dict[str, Any] | None = None
    exchange_response: Any = None
    risk_decision: RiskDecision | None = None
    signal: StrategySignal | None = None
    metadata: dict[str, Any] | None = None
    safety_failure: bool = False
    submitted_to_exchange: bool = False
    requires_manual_reconciliation: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.risk_decision is not None:
            data["risk_decision"] = self.risk_decision.to_dict()
        if self.signal is not None:
            data["signal"] = self.signal.to_dict()
        return convert_for_json(data)


class LiveExecutionEngine:
    def __init__(
        self,
        client: CoinDCXFuturesClient,
        settings: Settings,
        *,
        dry_run: bool | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.dry_run = settings.live_pilot_dry_run if dry_run is None else dry_run
        self.sync = LiveExchangeSynchronizer(client, settings)

    def process_decision(self, decision: RiskDecision) -> LiveExecutionReport:
        signal = decision.signal
        if not decision.approved:
            return _rejected(
                decision,
                f"Risk decision rejected: {decision.reason}",
                dry_run=self.dry_run,
            )
        
        # FINAL SAFETY GATE
        if not self.dry_run:
            if self.settings.trading_mode != "live" or not self.settings.live_trading_enabled:
                 return _rejected(decision, "REAL LIVE TRADING BLOCKED: TRADING_MODE must be 'live' and LIVE_TRADING_ENABLED must be true.", dry_run=False)
            
            if self.settings.live_confirm_i_understand_risk.strip().upper() != "YES":
                 return _rejected(decision, "REAL LIVE TRADING BLOCKED: LIVE_CONFIRM_I_UNDERSTAND_RISK must be set to 'YES' in .env", dry_run=False)

        if is_entry_signal(signal):
            try:
                order = build_live_entry_order(
                    decision,
                    margin_currency=self.settings.futures_margin_currency,
                    position_margin_type=self.settings.live_position_margin_type,
                )
            except ValueError as exc:
                return _rejected(decision, str(exc), dry_run=self.dry_run)

            order_payload = order.to_api_order()
            if self.dry_run:
                return LiveExecutionReport(
                    accepted=True,
                    dry_run=True,
                    reason="Live pilot dry-run: order was built but not sent.",
                    order_request=order_payload,
                    risk_decision=decision,
                    signal=signal,
                    metadata={"live_pilot": True},
                )
            if not self.settings.live_trading_allowed:
                return _rejected(
                    decision,
                    "Live order not sent: TRADING_MODE=live and LIVE_TRADING_ENABLED=true are required.",
                    dry_run=False,
                )
            preflight = self.sync.validate_no_conflicting_position(decision)
            if preflight is not None:
                return _rejected(decision, preflight, dry_run=False)
            try:
                response = self.client.place_order(order)
            except CoinDCXAPIError as exc:
                # 400 errors from CoinDCX are usually validation errors (Quantity, Notional, Tick)
                # These are non-retryable for the same signal.
                is_retryable = exc.status_code in (429, 500, 502, 503, 504)
                logger.error("[%s] Exchange rejected order (code=%s): %s", signal.pair, exc.status_code, str(exc))
                return LiveExecutionReport(
                    accepted=False,
                    dry_run=False,
                    reason=f"ORDER_REJECTED_NON_RETRYABLE: {str(exc)}" if not is_retryable else f"ORDER_REJECTED_RETRYABLE: {str(exc)}",
                    order_request=order_payload,
                    exchange_response=exc.response_text,
                    risk_decision=decision,
                    signal=signal,
                    metadata={"live_pilot": True, "exchange_rejected": True, "retryable": is_retryable, "error_code": exc.status_code},
                )
            except Exception as exc:
                logger.error("[%s] Unhandled exception during order placement: %s", signal.pair, exc)
                return _rejected(decision, f"EXECUTION_ERROR: {exc}", dry_run=False)

            sync_result = self.sync.sync_after_entry(response, decision)
            return LiveExecutionReport(
                accepted=sync_result.ok,
                dry_run=False,
                reason=(
                    "Live futures order submitted."
                    if sync_result.ok
                    else sync_result.reason
                ),
                order_request=order_payload,
                exchange_response=response,
                risk_decision=decision,
                signal=signal,
                metadata={"live_pilot": True, "live_sync": sync_result.to_dict()},
                safety_failure=sync_result.safety_failure,
                submitted_to_exchange=sync_result.submitted_to_exchange,
                requires_manual_reconciliation=sync_result.safety_failure and sync_result.submitted_to_exchange,
            )

        if is_exit_signal(signal):
            if self.dry_run:
                position_id = str(signal.metadata.get("live_position_id") or "").strip()
                return LiveExecutionReport(
                    accepted=True,
                    dry_run=True,
                    reason="Live pilot dry-run: position exit was built but not sent.",
                    order_request={
                        "pair": signal.pair,
                        "position_id": position_id or "<lookup-by-pair>",
                        "action": "exit_position",
                    },
                    risk_decision=decision,
                    signal=signal,
                    metadata={"live_pilot": True},
                )
            if not self.settings.live_trading_allowed:
                return _rejected(
                    decision,
                    "Live exit not sent: TRADING_MODE=live and LIVE_TRADING_ENABLED=true are required.",
                    dry_run=False,
                )
            sync_result = self.sync.exit_position_for_signal(signal)
            return LiveExecutionReport(
                accepted=sync_result.ok,
                dry_run=False,
                reason=sync_result.reason,
                order_request={
                    "pair": signal.pair,
                    "position_id": (
                        sync_result.position.position_id
                        if sync_result.position is not None
                        else str(signal.metadata.get("live_position_id") or "")
                    ),
                    "action": "exit_position",
                },
                exchange_response=sync_result.exit_response,
                risk_decision=decision,
                signal=signal,
                metadata={"live_pilot": True, "live_sync": sync_result.to_dict()},
                safety_failure=sync_result.safety_failure,
                submitted_to_exchange=sync_result.submitted_to_exchange,
                requires_manual_reconciliation=sync_result.safety_failure and sync_result.submitted_to_exchange,
            )

        return _rejected(
            decision,
            "Signal does not map to a live execution action.",
            dry_run=self.dry_run,
        )


    def update_tpsl(
        self,
        pair: str,
        *,
        take_profit: Decimal | None = None,
        stop_loss: Decimal | None = None,
        tick_size: Decimal | None = None,
        reason: str = "dynamic_adjustment",
    ) -> LiveExecutionReport:
        position = self.sync.fetch_position(pair)
        if position is None or not position.is_open:
            return LiveExecutionReport(
                accepted=False,
                dry_run=self.dry_run,
                reason=f"No open position found for {pair} to update TPSL.",
            )

        # Final safety normalization if tick_size provided
        if tick_size and tick_size > 0:
             from app.risk.exchange_rules import round_price
             mode = "down" if position.direction == SignalDirection.LONG else "up"
             if stop_loss:
                  stop_loss = round_price(stop_loss, tick_size, mode)
             if take_profit:
                  # For TP, Long rounds UP (better), Short rounds DOWN (better)
                  tp_mode = "up" if position.direction == SignalDirection.LONG else "down"
                  take_profit = round_price(take_profit, tick_size, tp_mode)

        if self.dry_run:
            return LiveExecutionReport(
                accepted=True,
                dry_run=True,
                reason=f"Live pilot dry-run: TPSL update for {pair} was built but not sent.",
                order_request={
                    "position_id": position.position_id,
                    "take_profit": str(take_profit) if take_profit else None,
                    "stop_loss": str(stop_loss) if stop_loss else None,
                    "reason": reason,
                },
                metadata={"live_pilot": True},
            )

        if not self.settings.live_trading_allowed:
             return LiveExecutionReport(
                accepted=False,
                dry_run=False,
                reason="Live trading disabled in settings.",
            )

        try:
            response = self.client.create_position_tpsl(
                position_id=position.position_id,
                take_profit_stop_price=take_profit,
                stop_loss_stop_price=stop_loss,
            )
            return LiveExecutionReport(
                accepted=True,
                dry_run=False,
                reason=f"Live TPSL update submitted for {pair}. Reason: {reason}",
                exchange_response=response,
                metadata={"live_pilot": True},
            )
        except CoinDCXAPIError as exc:
            is_retryable = exc.status_code in (429, 500, 502, 503, 504)
            return LiveExecutionReport(
                accepted=False,
                dry_run=False,
                reason=f"Live TPSL update REJECTED (code={exc.status_code}): {str(exc)}",
                metadata={"live_pilot": True, "retryable": is_retryable, "error_code": exc.status_code},
            )
        except Exception as exc:
            return LiveExecutionReport(
                accepted=False,
                dry_run=False,
                reason=f"Live TPSL update failed: {exc}",
                metadata={"live_pilot": True},
            )


def build_live_entry_order(
    decision: RiskDecision,
    *,
    margin_currency: str = "INR",
    position_margin_type: str = "isolated",
) -> FuturesOrderRequest:
    signal = decision.signal
    if not decision.approved:
        raise ValueError(f"Risk decision is not approved: {decision.reason}")
    if not is_entry_signal(signal):
        raise ValueError("Live entry order requires an entry signal.")
    if signal.direction is None:
        raise ValueError("Live entry signal is missing direction.")
    if decision.position_size is None or decision.position_size <= 0:
        raise ValueError("Live entry requires a positive approved position size.")
    if decision.leverage is None or decision.leverage <= 0:
        raise ValueError("Live entry requires a positive approved leverage.")
    if decision.leverage != decision.leverage.to_integral_value():
        raise ValueError("CoinDCX futures leverage must be an integer.")
    if signal.stop_loss is None or signal.stop_loss <= 0:
        raise ValueError("Live entry requires an initial stop loss.")
    if signal.entry_price is not None:
        _validate_stop_side(signal)

    return FuturesOrderRequest(
        side=_entry_side(signal.action),
        pair=signal.pair,
        order_type="market_order",
        total_quantity=decision.position_size,
        leverage=int(decision.leverage),
        time_in_force=None,
        margin_currency_short_name=margin_currency,
        position_margin_type=position_margin_type,
        take_profit_price=signal.take_profit,
        stop_loss_price=signal.stop_loss,
    )


class LiveExchangeSynchronizer:
    def __init__(self, client: CoinDCXFuturesClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def fetch_position(self, pair: str) -> LivePositionSnapshot | None:
        rows = self.client.list_positions(
            page=1,
            size=50,
            margin_currencies=[self.settings.futures_margin_currency],
            pairs=[pair],
        )
        positions = [
            LivePositionSnapshot.from_mapping(row)
            for row in rows
            if str(row.get("pair") or "") == pair
        ]
        if not positions:
            return None
        open_positions = [position for position in positions if position.is_open]
        return open_positions[0] if open_positions else positions[0]

    def fetch_orders(
        self,
        *,
        side: str,
        order_ids: set[str],
    ) -> list[LiveOrderSnapshot]:
        if not order_ids:
            return []
        rows = self.client.list_orders(
            status=ORDER_SYNC_STATUSES,
            side=side,
            page=1,
            size=100,
            margin_currencies=[self.settings.futures_margin_currency],
        )
        return [
            LiveOrderSnapshot.from_mapping(row)
            for row in rows
            if str(row.get("id") or "") in order_ids
        ]

    def validate_no_conflicting_position(self, decision: RiskDecision) -> str | None:
        signal = decision.signal
        position = self.fetch_position(signal.pair)
        if position is None or not position.is_open:
            return None
        return (
            "live_position_conflict: exchange already has an open "
            f"{position.direction.value if position.direction else 'unknown'} "
            f"position for {signal.pair} qty={position.quantity}. "
            "Stop the bot and reconcile manually before opening another live trade."
        )

    def sync_after_entry(
        self,
        exchange_response: Any,
        decision: RiskDecision,
    ) -> LiveSyncResult:
        signal = decision.signal
        pair = signal.pair
        order_ids = _extract_order_ids(exchange_response)
        side = _entry_side(signal.action)
        
        logger.info("[%s] Order submitted (ids=%s). Syncing protective TPSL...", pair, order_ids)
        
        # Wait briefly for exchange to process
        import time
        time.sleep(1.5)
        
        try:
            orders = self.fetch_orders(side=side, order_ids=order_ids)
            position = self.fetch_position(pair)
            
            if position is None:
                return LiveSyncResult(
                    ok=False,
                    reason="Live entry submitted but no exchange position row was returned.",
                    orders=orders,
                )

            tpsl_response = None
            if position.is_open and _position_needs_tpsl_sync(position, signal):
                try:
                    logger.info("[%s] Creating exchange-side TPSL: SL=%.2f, TP=%.2f", 
                                pair, signal.stop_loss or 0, signal.take_profit or 0)
                    tpsl_response = self.client.create_position_tpsl(
                        position_id=position.position_id,
                        take_profit_stop_price=signal.take_profit,
                        stop_loss_stop_price=signal.stop_loss,
                    )
                    # Refresh position to see new triggers
                    position = self.fetch_position(pair) or position
                except Exception as exc:
                    logger.error("[%s] SAFETY FAILURE: Order placed but TPSL creation FAILED: %s", pair, exc)
                    return LiveSyncResult(
                        ok=False, 
                        reason=f"SAFETY FAILURE: Position is OPEN but protective TPSL sync failed: {exc}",
                        orders=orders,
                        position=position,
                        safety_failure=True,
                        submitted_to_exchange=True,
                    )

            if not position.is_open:
                return LiveSyncResult(
                    ok=False,
                    reason="Live entry submitted but exchange position is not open yet.",
                    orders=orders,
                    position=position,
                    tpsl_response=tpsl_response,
                    submitted_to_exchange=True,
                )
            
            if signal.direction is not None and position.direction != signal.direction:
                return LiveSyncResult(
                    ok=False,
                    reason=(
                        "Live entry direction mismatch after sync: expected "
                        f"{signal.direction.value}, exchange has "
                        f"{position.direction.value if position.direction else 'none'}."
                    ),
                    orders=orders,
                    position=position,
                    tpsl_response=tpsl_response,
                    submitted_to_exchange=True,
                    safety_failure=True, # Direction mismatch is a safety issue
                )
                
            if signal.stop_loss is not None and not position.has_stop_loss:
                # If we required stop loss sync but it's missing, it's a safety failure
                return LiveSyncResult(
                    ok=False,
                    reason="SAFETY FAILURE: Live entry is open but exchange stop-loss trigger is missing.",
                    orders=orders,
                    position=position,
                    tpsl_response=tpsl_response,
                    safety_failure=True,
                    submitted_to_exchange=True,
                )

            return LiveSyncResult(
                ok=True,
                orders=orders,
                position=position,
                tpsl_response=tpsl_response,
                reason="Live entry reconciled with exchange position.",
                submitted_to_exchange=True,
            )
        except Exception as exc:
            logger.error("[%s] SAFETY FAILURE: Exception during post-entry sync: %s", pair, exc)
            return LiveSyncResult(
                ok=False, 
                reason=f"SAFETY FAILURE: Post-entry sync exception: {exc}",
                submitted_to_exchange=True, # Assume submitted if we got this far
                safety_failure=True,
            )

    def exit_position_for_signal(self, signal: StrategySignal) -> LiveSyncResult:
        position_id = str(signal.metadata.get("live_position_id") or "").strip()
        if position_id:
            rows = self.client.list_positions(
                page=1,
                size=50,
                margin_currencies=[self.settings.futures_margin_currency],
                position_ids=[position_id],
            )
            position = (
                LivePositionSnapshot.from_mapping(rows[0])
                if rows
                else None
            )
        else:
            position = self.fetch_position(signal.pair)
        if position is None or not position.is_open:
            return LiveSyncResult(
                ok=False,
                reason=f"No open exchange position found for live exit: {signal.pair}.",
                orders=[],
                position=position,
            )

        expected_direction = (
            SignalDirection.LONG
            if signal.action == SignalAction.EXIT_LONG
            else SignalDirection.SHORT
        )
        if position.direction != expected_direction:
            return LiveSyncResult(
                ok=False,
                reason=(
                    "Live exit direction mismatch: signal wants to close "
                    f"{expected_direction.value}, exchange has "
                    f"{position.direction.value if position.direction else 'none'}."
                ),
                orders=[],
                position=position,
            )

        exit_response = self.client.exit_position(position.position_id)
        updated = self.fetch_position(position.pair)
        cancel_response = None
        if updated is not None and not updated.is_open:
            cancel_response = self.client.cancel_all_open_orders_for_position(
                position.position_id
            )
        return LiveSyncResult(
            ok=updated is not None and not updated.is_open,
            reason=(
                "Live futures position exit submitted and exchange position is closed."
                if updated is not None and not updated.is_open
                else "Live futures position exit submitted but exchange still reports an open position."
            ),
            orders=[],
            position=updated or position,
            exit_response=exit_response,
            cancel_response=cancel_response,
        )


def _entry_side(action: SignalAction) -> str:
    if action == SignalAction.ENTER_LONG:
        return "buy"
    if action == SignalAction.ENTER_SHORT:
        return "sell"
    raise ValueError(f"Unsupported live entry action: {action.value}")


def _validate_stop_side(signal: StrategySignal) -> None:
    assert signal.entry_price is not None
    assert signal.stop_loss is not None
    if signal.direction == SignalDirection.LONG and signal.stop_loss >= signal.entry_price:
        raise ValueError("Long live entry stop loss must be below entry price.")
    if signal.direction == SignalDirection.SHORT and signal.stop_loss <= signal.entry_price:
        raise ValueError("Short live entry stop loss must be above entry price.")


def _rejected(
    decision: RiskDecision,
    reason: str,
    *,
    dry_run: bool,
) -> LiveExecutionReport:
    return LiveExecutionReport(
        accepted=False,
        dry_run=dry_run,
        reason=reason,
        risk_decision=decision,
        signal=decision.signal,
    )


def _extract_order_ids(exchange_response: Any) -> set[str]:
    rows: list[Any]
    if isinstance(exchange_response, list):
        rows = exchange_response
    else:
        rows = [exchange_response]
    ids = {
        str(row.get("id"))
        for row in rows
        if isinstance(row, dict) and row.get("id") not in (None, "")
    }
    return ids


def _position_needs_tpsl_sync(
    position: LivePositionSnapshot,
    signal: StrategySignal,
) -> bool:
    if not position.is_open:
        return False
    stop_missing = signal.stop_loss is not None and not position.has_stop_loss
    tp_missing = signal.take_profit is not None and not position.has_take_profit
    return stop_missing or tp_missing


def _decimal_or_zero(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    return Decimal(str(value))


def _positive_decimal_or_none(value: Any) -> Decimal | None:
    decimal_value = _decimal_or_zero(value)
    return decimal_value if decimal_value > 0 else None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
