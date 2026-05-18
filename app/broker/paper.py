from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Mapping

from app.broker.models import (
    PaperAccountSnapshot,
    PaperExecutionReport,
    PaperExecutionStatus,
    PaperFill,
    PaperOrder,
    PaperOrderSide,
    PaperOrderStatus,
    PaperPosition,
)
from app.data.candle_builder import OHLCVCandle
from app.fees import effective_fee_rate
from app.risk.limits import is_entry_signal, is_exit_signal
from app.risk.models import RiskDecision
from app.strategies.base import SignalAction, SignalDirection, StrategySignal
from app.utils.time import utc_timestamp_ms


@dataclass(frozen=True)
class _ExitTrigger:
    action: SignalAction
    price: Decimal
    reason: str
    metadata: dict[str, object]


class PaperBroker:
    def __init__(
        self,
        *,
        starting_equity: Decimal,
        maker_fee_rate: Decimal | None = None,
        taker_fee_rate: Decimal = Decimal("0"),
        fee_gst_rate: Decimal = Decimal("0"),
        entry_fee_type: str = "maker",
        exit_fee_type: str = "taker",
        slippage_pct: Decimal = Decimal("0"),
        stop_slippage_pct: Decimal | None = None,
        trailing_stop_enabled: bool = False,
        trailing_stop_activation_pct: Decimal = Decimal("1"),
        trailing_stop_distance_pct: Decimal = Decimal("2"),
        logger: logging.Logger | None = None,
    ) -> None:
        if starting_equity <= 0:
            raise ValueError("starting_equity must be positive.")
        if maker_fee_rate is None:
            maker_fee_rate = taker_fee_rate
        if maker_fee_rate < 0:
            raise ValueError("maker_fee_rate cannot be negative.")
        if taker_fee_rate < 0:
            raise ValueError("taker_fee_rate cannot be negative.")
        if fee_gst_rate < 0:
            raise ValueError("fee_gst_rate cannot be negative.")
        entry_fee_type = _normalize_fee_type(entry_fee_type)
        exit_fee_type = _normalize_fee_type(exit_fee_type)
        if slippage_pct < 0:
            raise ValueError("slippage_pct cannot be negative.")
        if stop_slippage_pct is not None and stop_slippage_pct < 0:
            raise ValueError("stop_slippage_pct cannot be negative.")
        if trailing_stop_activation_pct < 0:
            raise ValueError("trailing_stop_activation_pct cannot be negative.")
        if trailing_stop_distance_pct < 0:
            raise ValueError("trailing_stop_distance_pct cannot be negative.")
        if trailing_stop_enabled and trailing_stop_distance_pct <= 0:
            raise ValueError("trailing_stop_distance_pct must be positive when enabled.")

        self.starting_equity = starting_equity
        self.maker_fee_rate = maker_fee_rate
        self.taker_fee_rate = taker_fee_rate
        self.fee_gst_rate = fee_gst_rate
        self.entry_fee_type = entry_fee_type
        self.exit_fee_type = exit_fee_type
        self.slippage_pct = slippage_pct
        self.stop_slippage_pct = stop_slippage_pct
        self.trailing_stop_enabled = trailing_stop_enabled
        self.trailing_stop_activation_pct = trailing_stop_activation_pct
        self.trailing_stop_distance_pct = trailing_stop_distance_pct
        self.logger = logger or logging.getLogger(__name__)
        self.realized_pnl = Decimal("0")
        self.fees_paid = Decimal("0")
        self.funding_paid = Decimal("0")
        self.positions: dict[str, PaperPosition] = {}
        self.orders: list[PaperOrder] = []
        self.fills: list[PaperFill] = []
        self._order_sequence = 0
        self._fill_sequence = 0

    def execute_decision(
        self,
        decision: RiskDecision,
        *,
        market_price: Decimal | None = None,
        timestamp_ms: int | None = None,
    ) -> PaperExecutionReport:
        timestamp = utc_timestamp_ms() if timestamp_ms is None else timestamp_ms
        signal = decision.signal

        if not decision.approved:
            return self._rejected_report(
                reason=f"Risk decision rejected: {decision.reason}",
                timestamp_ms=timestamp,
                risk_decision=decision,
                signal=signal,
                market_price=market_price,
            )

        if is_entry_signal(signal):
            return self._open_from_decision(
                decision,
                market_price=market_price,
                timestamp_ms=timestamp,
            )

        if is_exit_signal(signal):
            return self._close_from_signal(
                signal,
                market_price=market_price,
                timestamp_ms=timestamp,
                risk_decision=decision,
                reason=signal.reason or "Exit signal filled by paper broker.",
            )

        return self._rejected_report(
            reason="Signal does not map to a paper execution action.",
            timestamp_ms=timestamp,
            risk_decision=decision,
            signal=signal,
            market_price=market_price,
        )

    def process_candle(self, candle: OHLCVCandle) -> list[PaperExecutionReport]:
        reports: list[PaperExecutionReport] = []
        position = self.positions.get(candle.pair)
        if position is None:
            return reports

        trigger = self._trigger_for_position(position, candle)
        if trigger is None:
            self._update_trailing_stop(position, candle)
            return reports

        signal = StrategySignal(
            strategy_name=position.strategy_name,
            pair=position.pair,
            interval=candle.interval,
            action=trigger.action,
            direction=position.direction,
            confidence=Decimal("1"),
            reason=trigger.reason,
            timestamp_ms=candle.close_time_ms,
            entry_price=trigger.price,
            metadata={"paper_trigger": True, **trigger.metadata},
        )
        reports.append(
            self._close_from_signal(
                signal,
                market_price=trigger.price,
                timestamp_ms=candle.close_time_ms,
                risk_decision=None,
                reason=trigger.reason,
            )
        )
        return reports

    def apply_funding(
        self,
        *,
        mark_prices: Mapping[str, Decimal],
        funding_fee_rate: Decimal,
    ) -> Decimal:
        if funding_fee_rate == 0:
            return Decimal("0")

        funding_paid = Decimal("0")
        for pair, position in self.positions.items():
            mark_price = mark_prices.get(pair, position.entry_price)
            if mark_price <= 0:
                continue
            notional = abs(position.quantity * mark_price)
            if position.direction == SignalDirection.LONG:
                funding_paid += notional * funding_fee_rate
            else:
                funding_paid -= notional * funding_fee_rate

        self.funding_paid += funding_paid
        return funding_paid

    def update_dynamic_atr_exits(
        self,
        candle: OHLCVCandle,
        *,
        atr: Decimal | None,
        stop_multiple: Decimal,
        take_profit_multiple: Decimal,
        stop_enabled: bool = True,
        take_profit_enabled: bool = True,
        trailing_enabled: bool = True,
        take_profit_mode: str = "fixed",
    ) -> None:
        if atr is None or atr <= 0:
            return

        position = self.positions.get(candle.pair)
        if position is None:
            return
        if not position.metadata.get("atr_dynamic_exits_enabled"):
            return
        if position.opened_at_ms >= candle.close_time_ms:
            return

        stop_enabled = _bool_metadata(position.metadata, "atr_stop_enabled", stop_enabled)
        take_profit_enabled = _bool_metadata(
            position.metadata,
            "atr_take_profit_enabled",
            take_profit_enabled,
        )
        trailing_enabled = _bool_metadata(
            position.metadata,
            "atr_trailing_enabled",
            trailing_enabled,
        )
        if not stop_enabled and not take_profit_enabled:
            return

        stop_multiple = _decimal_metadata(
            position.metadata,
            "atr_stop_multiple",
            stop_multiple,
        )
        trailing_multiple = _decimal_metadata(
            position.metadata,
            "atr_trailing_multiple",
            _decimal_metadata(
                position.metadata,
                "trailing_atr_multiple",
                stop_multiple,
            ),
        )
        take_profit_multiple = _decimal_metadata(
            position.metadata,
            "atr_take_profit_multiple",
            take_profit_multiple,
        )
        if stop_multiple <= 0:
            stop_enabled = False
        if take_profit_multiple <= 0:
            take_profit_enabled = False
        if trailing_multiple <= 0:
            trailing_multiple = stop_multiple
        if not stop_enabled and not take_profit_enabled:
            return

        stop_distance = atr * stop_multiple
        trailing_distance = atr * trailing_multiple
        target_distance = atr * take_profit_multiple
        tiny_price = Decimal("0.00000001")
        take_profit_mode = str(
            position.metadata.get("atr_take_profit_mode", take_profit_mode)
        ).strip().lower()
        if take_profit_mode not in {
            "fixed",
            "entry_atr",
            "ratchet",
            "trailing_atr",
            "none",
        }:
            take_profit_mode = "fixed"
        if not take_profit_enabled:
            take_profit_mode = "none"

        if position.direction == SignalDirection.LONG:
            if trailing_enabled:
                best_price = max(
                    _decimal_metadata(position.metadata, "atr_best_price", position.entry_price),
                    candle.high,
                    candle.close,
                    position.entry_price,
                )
            else:
                best_price = position.entry_price
            stop_candidate = position.stop_loss
            if stop_enabled:
                distance = trailing_distance if trailing_enabled else stop_distance
                stop_candidate = max(best_price - distance, tiny_price)
                if position.stop_loss is not None:
                    stop_candidate = max(position.stop_loss, stop_candidate)
            entry_target = max(position.entry_price + target_distance, tiny_price)
            if take_profit_mode == "none":
                take_profit = position.take_profit if position.metadata.get("manual_take_profit_pct") else None
            elif take_profit_mode == "fixed":
                take_profit = position.take_profit
            elif take_profit_mode == "entry_atr":
                take_profit = entry_target
            elif take_profit_mode == "ratchet":
                ratchet_target = max(best_price + target_distance, tiny_price)
                take_profit = max(position.take_profit or entry_target, ratchet_target)
            else:
                take_profit = max(candle.close + target_distance, tiny_price)
        else:
            if trailing_enabled:
                best_price = min(
                    _decimal_metadata(position.metadata, "atr_best_price", position.entry_price),
                    candle.low,
                    candle.close,
                    position.entry_price,
                )
            else:
                best_price = position.entry_price
            stop_candidate = position.stop_loss
            if stop_enabled:
                distance = trailing_distance if trailing_enabled else stop_distance
                stop_candidate = best_price + distance
                if position.stop_loss is not None:
                    stop_candidate = min(position.stop_loss, stop_candidate)
            entry_target = max(position.entry_price - target_distance, tiny_price)
            if take_profit_mode == "none":
                take_profit = position.take_profit if position.metadata.get("manual_take_profit_pct") else None
            elif take_profit_mode == "fixed":
                take_profit = position.take_profit
            elif take_profit_mode == "entry_atr":
                take_profit = entry_target
            elif take_profit_mode == "ratchet":
                ratchet_target = max(best_price - target_distance, tiny_price)
                take_profit = min(position.take_profit or entry_target, ratchet_target)
            else:
                take_profit = max(candle.close - target_distance, tiny_price)

        metadata = {
            **position.metadata,
            "atr_dynamic_exit_active": True,
            "atr_latest": atr,
            "atr_stop_enabled": stop_enabled,
            "atr_take_profit_enabled": take_profit_enabled,
            "atr_trailing_enabled": trailing_enabled,
            "atr_stop_multiple": stop_multiple,
            "atr_take_profit_multiple": take_profit_multiple,
            "atr_trailing_multiple": trailing_multiple,
            "trailing_atr_multiple": trailing_multiple,
            "atr_take_profit_mode": take_profit_mode,
            "atr_best_price": best_price,
            "atr_stop_loss": stop_candidate,
            "atr_take_profit": take_profit,
            "atr_exit_updated_at_ms": candle.close_time_ms,
            "atr_exit_update_count": int(
                position.metadata.get("atr_exit_update_count", 0)
            )
            + 1,
        }
        self.positions[position.pair] = replace(
            position,
            stop_loss=stop_candidate,
            take_profit=take_profit,
            updated_at_ms=candle.close_time_ms,
            metadata=metadata,
        )

    def snapshot(
        self,
        mark_prices: Mapping[str, Decimal] | None = None,
    ) -> PaperAccountSnapshot:
        unrealized = Decimal("0")
        for pair, position in self.positions.items():
            mark_price = mark_prices.get(pair) if mark_prices else None
            if mark_price is not None:
                unrealized += position.unrealized_pnl(mark_price)

        open_notional = Decimal("0")
        for pair, position in self.positions.items():
            mark_price = mark_prices.get(pair) if mark_prices else None
            price = mark_price if mark_price is not None else position.entry_price
            open_notional += abs(position.quantity * price)

        equity = (
            self.starting_equity
            + self.realized_pnl
            + unrealized
            - self.fees_paid
            - self.funding_paid
        )
        return PaperAccountSnapshot(
            starting_equity=self.starting_equity,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            fees_paid=self.fees_paid,
            equity=equity,
            open_position_count=len(self.positions),
            open_notional=open_notional,
            funding_paid=self.funding_paid,
        )

    def open_positions(self) -> list[PaperPosition]:
        return list(self.positions.values())

    def _open_from_decision(
        self,
        decision: RiskDecision,
        *,
        market_price: Decimal | None,
        timestamp_ms: int,
    ) -> PaperExecutionReport:
        signal = decision.signal
        if decision.position_size is None or decision.position_size <= 0:
            return self._rejected_report(
                reason="Approved risk decision is missing a positive position size.",
                timestamp_ms=timestamp_ms,
                risk_decision=decision,
                signal=signal,
                market_price=market_price,
            )
        if signal.direction is None:
            return self._rejected_report(
                reason="Entry signal is missing direction.",
                timestamp_ms=timestamp_ms,
                risk_decision=decision,
                signal=signal,
                market_price=market_price,
            )
        existing_position = self.positions.get(signal.pair)
        if existing_position is not None:
            if _can_scale_in(signal, existing_position):
                return self._scale_position_from_decision(
                    decision,
                    position=existing_position,
                    market_price=market_price,
                    timestamp_ms=timestamp_ms,
                )
            return self._rejected_report(
                reason=f"Paper position already open for {signal.pair}.",
                timestamp_ms=timestamp_ms,
                risk_decision=decision,
                signal=signal,
                market_price=market_price,
            )

        side = _entry_side(signal.action)
        if side is None:
            return self._rejected_report(
                reason="Unsupported entry action.",
                timestamp_ms=timestamp_ms,
                risk_decision=decision,
                signal=signal,
                market_price=market_price,
            )

        base_price = market_price or signal.entry_price
        if base_price is None or base_price <= 0:
            return self._rejected_report(
                reason="A positive market price or signal entry price is required.",
                timestamp_ms=timestamp_ms,
                risk_decision=decision,
                signal=signal,
                market_price=market_price,
            )

        fill_price = self._apply_slippage(base_price, side, self.slippage_pct)
        leverage = decision.leverage or Decimal("1")
        quantity = decision.position_size
        margin_error, margin_metadata = self._entry_margin_check(
            decision=decision,
            quantity=quantity,
            price=fill_price,
            leverage=leverage,
            mark_price=base_price,
        )
        if margin_error is not None:
            return self._rejected_report(
                reason=margin_error,
                timestamp_ms=timestamp_ms,
                risk_decision=decision,
                signal=signal,
                market_price=market_price,
            )
        order = self._filled_order(
            pair=signal.pair,
            side=side,
            action=signal.action,
            quantity=quantity,
            price=fill_price,
            leverage=leverage,
            timestamp_ms=timestamp_ms,
            reason="Paper entry filled.",
        )
        fee_type = self._fee_type_for_signal(signal, default_fee_type=self.entry_fee_type)
        fee_details = self._fee_details(order.notional, fee_type)
        fee = fee_details["total_fee"]
        fill = self._fill(
            order=order,
            fee=fee,
            timestamp_ms=timestamp_ms,
            metadata=fee_details,
        )
        self.fees_paid += fee

        position = PaperPosition(
            pair=signal.pair,
            direction=signal.direction,
            quantity=quantity,
            entry_price=fill_price,
            leverage=leverage,
            opened_at_ms=timestamp_ms,
            updated_at_ms=timestamp_ms,
            strategy_name=signal.strategy_name,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            metadata={
                **decision.metadata,
                **signal.metadata,
                "risk_max_loss": decision.max_loss,
                "initial_stop_loss": signal.stop_loss,
                **margin_metadata,
                "trailing_stop_enabled": self.trailing_stop_enabled,
                "trailing_stop_activation_pct": self.trailing_stop_activation_pct,
                "trailing_stop_distance_pct": self.trailing_stop_distance_pct,
                "entry_fee_type": fee_type,
                "entry_fee_rate": fee_details["fee_rate"],
                "entry_fee_gst_rate": self.fee_gst_rate,
                "entry_effective_fee_rate": fee_details["effective_fee_rate"],
            },
        )
        self.positions[signal.pair] = position
        self.orders.append(order)
        self.fills.append(fill)

        return PaperExecutionReport(
            accepted=True,
            status=PaperExecutionStatus.FILLED,
            reason="Paper entry filled.",
            order=order,
            fill=fill,
            position=position,
            risk_decision=decision,
            signal=signal,
            account=self.snapshot({signal.pair: fill_price}),
        )

    def _scale_position_from_decision(
        self,
        decision: RiskDecision,
        *,
        position: PaperPosition,
        market_price: Decimal | None,
        timestamp_ms: int,
    ) -> PaperExecutionReport:
        signal = decision.signal
        if decision.position_size is None or decision.position_size <= 0:
            return self._rejected_report(
                reason="Approved scale-in decision is missing a positive position size.",
                timestamp_ms=timestamp_ms,
                risk_decision=decision,
                signal=signal,
                market_price=market_price,
            )

        side = _entry_side(signal.action)
        if side is None:
            return self._rejected_report(
                reason="Unsupported scale-in action.",
                timestamp_ms=timestamp_ms,
                risk_decision=decision,
                signal=signal,
                market_price=market_price,
            )

        base_price = market_price or signal.entry_price
        if base_price is None or base_price <= 0:
            return self._rejected_report(
                reason="A positive market price or signal entry price is required.",
                timestamp_ms=timestamp_ms,
                risk_decision=decision,
                signal=signal,
                market_price=market_price,
            )

        fill_price = self._apply_slippage(base_price, side, self.slippage_pct)
        leverage = decision.leverage or position.leverage
        quantity = decision.position_size
        margin_error, margin_metadata = self._entry_margin_check(
            decision=decision,
            quantity=quantity,
            price=fill_price,
            leverage=leverage,
            mark_price=base_price,
        )
        if margin_error is not None:
            return self._rejected_report(
                reason=margin_error,
                timestamp_ms=timestamp_ms,
                risk_decision=decision,
                signal=signal,
                market_price=market_price,
            )
        order = self._filled_order(
            pair=signal.pair,
            side=side,
            action=signal.action,
            quantity=quantity,
            price=fill_price,
            leverage=leverage,
            timestamp_ms=timestamp_ms,
            reason="Paper grid scale-in filled.",
        )
        fee_type = self._fee_type_for_signal(signal, default_fee_type=self.entry_fee_type)
        fee_details = self._fee_details(order.notional, fee_type)
        fee = fee_details["total_fee"]
        fill = self._fill(
            order=order,
            fee=fee,
            timestamp_ms=timestamp_ms,
            metadata=fee_details,
        )
        self.fees_paid += fee

        new_quantity = position.quantity + quantity
        average_entry = (
            (position.quantity * position.entry_price) + (quantity * fill_price)
        ) / new_quantity
        metadata = {
            **position.metadata,
            **decision.metadata,
            **signal.metadata,
            "risk_max_loss": decision.max_loss,
            "scaled_in": True,
            **margin_metadata,
            "scale_in_count": int(position.metadata.get("scale_in_count", 0)) + 1,
            "last_scale_in_price": fill_price,
            "last_scale_in_at_ms": timestamp_ms,
            "last_scale_in_fee_type": fee_type,
            "last_scale_in_fee_rate": fee_details["fee_rate"],
            "last_scale_in_fee_gst_rate": self.fee_gst_rate,
            "last_scale_in_effective_fee_rate": fee_details["effective_fee_rate"],
        }
        updated_position = replace(
            position,
            quantity=new_quantity,
            entry_price=average_entry,
            leverage=leverage,
            updated_at_ms=timestamp_ms,
            strategy_name=signal.strategy_name,
            stop_loss=signal.stop_loss or position.stop_loss,
            take_profit=signal.take_profit,
            metadata=metadata,
        )
        self.positions[signal.pair] = updated_position
        self.orders.append(order)
        self.fills.append(fill)

        return PaperExecutionReport(
            accepted=True,
            status=PaperExecutionStatus.FILLED,
            reason="Paper grid scale-in filled.",
            order=order,
            fill=fill,
            position=updated_position,
            risk_decision=decision,
            signal=signal,
            account=self.snapshot({signal.pair: fill_price}),
            metadata={"scaled_position": True},
        )

    def _close_from_signal(
        self,
        signal: StrategySignal,
        *,
        market_price: Decimal | None,
        timestamp_ms: int,
        risk_decision: RiskDecision | None,
        reason: str,
    ) -> PaperExecutionReport:
        position = self.positions.get(signal.pair)
        if position is None:
            return self._rejected_report(
                reason=f"No open paper position for {signal.pair}.",
                timestamp_ms=timestamp_ms,
                risk_decision=risk_decision,
                signal=signal,
                market_price=market_price,
            )
        if not _exit_matches_position(signal.action, position.direction):
            return self._rejected_report(
                reason="Exit signal direction does not match the open position.",
                timestamp_ms=timestamp_ms,
                risk_decision=risk_decision,
                signal=signal,
                market_price=market_price,
            )

        side = _exit_side(position.direction)
        base_price = market_price or signal.entry_price
        if base_price is None or base_price <= 0:
            return self._rejected_report(
                reason="A positive market price or signal price is required to close.",
                timestamp_ms=timestamp_ms,
                risk_decision=risk_decision,
                signal=signal,
                market_price=market_price,
            )

        slippage_pct = self._exit_slippage_pct(signal)
        fill_price = self._apply_slippage(base_price, side, slippage_pct)
        action = (
            SignalAction.EXIT_LONG
            if position.direction == SignalDirection.LONG
            else SignalAction.EXIT_SHORT
        )
        order = self._filled_order(
            pair=position.pair,
            side=side,
            action=action,
            quantity=position.quantity,
            price=fill_price,
            leverage=position.leverage,
            timestamp_ms=timestamp_ms,
            reason=reason,
        )
        gross_pnl = position.unrealized_pnl(fill_price)
        fee_type = self._fee_type_for_signal(signal, default_fee_type=self.exit_fee_type)
        fee_details = self._fee_details(order.notional, fee_type)
        fee = fee_details["total_fee"]
        fill = self._fill(
            order=order,
            fee=fee,
            timestamp_ms=timestamp_ms,
            realized_pnl=gross_pnl,
            metadata={
                **fee_details,
                "pre_slippage_price": base_price,
                "slippage_pct": slippage_pct,
                "slippage_type": _slippage_type(signal),
                **_exit_fill_metadata(signal),
            },
        )
        self.realized_pnl += gross_pnl
        self.fees_paid += fee
        closed_position = self.positions.pop(position.pair)
        self.orders.append(order)
        self.fills.append(fill)

        return PaperExecutionReport(
            accepted=True,
            status=PaperExecutionStatus.FILLED,
            reason=reason,
            order=order,
            fill=fill,
            position=closed_position,
            risk_decision=risk_decision,
            signal=signal,
            account=self.snapshot({position.pair: fill_price}),
            metadata={"closed_position": True},
        )

    def _entry_margin_check(
        self,
        *,
        decision: RiskDecision,
        quantity: Decimal,
        price: Decimal,
        leverage: Decimal,
        mark_price: Decimal,
    ) -> tuple[str | None, dict[str, object]]:
        snapshot = self.snapshot({decision.signal.pair: mark_price})
        account_blown = snapshot.equity <= 0
        notional = abs(quantity * price)
        required_margin = notional / leverage if leverage > 0 else notional
        planned_risk = decision.max_loss or Decimal("0")
        max_notional = snapshot.equity * leverage if snapshot.equity > 0 else Decimal("0")
        margin_ok = (
            not account_blown
            and required_margin <= snapshot.equity
            and planned_risk <= snapshot.equity
            and notional <= max_notional
        )
        metadata: dict[str, object] = {
            "equity_before_trade": snapshot.equity,
            "available_equity": snapshot.equity,
            "required_margin": required_margin,
            "planned_risk_amount": planned_risk,
            "position_quantity": quantity,
            "position_notional": notional,
            "max_position_notional": max_notional,
            "margin_ok": margin_ok,
            "account_blown": account_blown,
        }
        if account_blown:
            return "Paper broker rejected entry: account equity is depleted.", metadata
        if required_margin > snapshot.equity:
            return (
                "Paper broker rejected entry: required margin exceeds available equity.",
                metadata,
            )
        if planned_risk > snapshot.equity:
            return (
                "Paper broker rejected entry: planned risk exceeds available equity.",
                metadata,
            )
        if notional > max_notional:
            return (
                "Paper broker rejected entry: position notional exceeds equity times leverage.",
                metadata,
            )
        return None, metadata

    def _filled_order(
        self,
        *,
        pair: str,
        side: PaperOrderSide,
        action: SignalAction,
        quantity: Decimal,
        price: Decimal,
        leverage: Decimal,
        timestamp_ms: int,
        reason: str,
    ) -> PaperOrder:
        self._order_sequence += 1
        return PaperOrder(
            order_id=f"paper-order-{self._order_sequence}",
            pair=pair,
            side=side,
            action=action,
            quantity=quantity,
            price=price,
            leverage=leverage,
            status=PaperOrderStatus.FILLED,
            created_at_ms=timestamp_ms,
            filled_at_ms=timestamp_ms,
            reason=reason,
        )

    def _fill(
        self,
        *,
        order: PaperOrder,
        fee: Decimal,
        timestamp_ms: int,
        realized_pnl: Decimal = Decimal("0"),
        metadata: dict[str, object] | None = None,
    ) -> PaperFill:
        self._fill_sequence += 1
        return PaperFill(
            fill_id=f"paper-fill-{self._fill_sequence}",
            order_id=order.order_id,
            pair=order.pair,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
            fee=fee,
            timestamp_ms=timestamp_ms,
            realized_pnl=realized_pnl,
            metadata=metadata or {},
        )

    def _rejected_report(
        self,
        *,
        reason: str,
        timestamp_ms: int,
        risk_decision: RiskDecision | None,
        signal: StrategySignal | None,
        market_price: Decimal | None = None,
    ) -> PaperExecutionReport:
        self.logger.info("Paper execution rejected: %s", reason)
        mark_prices = None
        if signal is not None and market_price is not None:
            mark_prices = {signal.pair: market_price}
        return PaperExecutionReport(
            accepted=False,
            status=PaperExecutionStatus.REJECTED,
            reason=reason,
            order=None,
            risk_decision=risk_decision,
            signal=signal,
            account=self.snapshot(mark_prices),
        )

    def _apply_slippage(
        self,
        price: Decimal,
        side: PaperOrderSide,
        slippage_pct: Decimal,
    ) -> Decimal:
        if slippage_pct == 0:
            return price
        adjustment = price * (slippage_pct / Decimal("100"))
        if side == PaperOrderSide.BUY:
            return price + adjustment
        return price - adjustment

    def _exit_slippage_pct(self, signal: StrategySignal) -> Decimal:
        if _is_stop_exit(signal) and self.stop_slippage_pct is not None:
            return self.stop_slippage_pct
        return self.slippage_pct

    def _fee_rate(self, fee_type: str) -> Decimal:
        if fee_type == "maker":
            return self.maker_fee_rate
        return self.taker_fee_rate

    def _fee_details(self, notional: Decimal, fee_type: str) -> dict[str, Decimal | str]:
        fee_rate = self._fee_rate(fee_type)
        base_fee = notional * fee_rate
        gst_fee = base_fee * self.fee_gst_rate
        total_fee = base_fee + gst_fee
        return {
            "fee_type": fee_type,
            "fee_rate": fee_rate,
            "fee_gst_rate": self.fee_gst_rate,
            "base_fee": base_fee,
            "gst_fee": gst_fee,
            "total_fee": total_fee,
            "effective_fee_rate": effective_fee_rate(fee_rate, self.fee_gst_rate),
        }

    def _fee_type_for_signal(
        self,
        signal: StrategySignal,
        *,
        default_fee_type: str,
    ) -> str:
        explicit = signal.metadata.get("fee_type")
        if explicit is None:
            return default_fee_type
        return _normalize_fee_type(str(explicit))

    def _trigger_for_position(
        self,
        position: PaperPosition,
        candle: OHLCVCandle,
    ) -> _ExitTrigger | None:
        if position.direction == SignalDirection.LONG:
            stop = position.stop_loss
            target = position.take_profit
            if stop is not None and candle.open <= stop:
                return _exit_trigger(
                    action=SignalAction.EXIT_LONG,
                    price=candle.open,
                    reason=_gap_stop_reason(position),
                    trigger_type="stop_loss",
                    trigger_level=stop,
                    gap_exit=True,
                )
            if target is not None and candle.open >= target:
                return _exit_trigger(
                    action=SignalAction.EXIT_LONG,
                    price=candle.open,
                    reason=_gap_take_profit_reason(position),
                    trigger_type="take_profit",
                    trigger_level=target,
                    gap_exit=True,
                )
            stop_hit = stop is not None and candle.low <= stop
            target_hit = target is not None and candle.high >= target
            if stop_hit and target_hit:
                assert stop is not None
                return _exit_trigger(
                    action=SignalAction.EXIT_LONG,
                    price=stop,
                    reason="Ambiguous candle hit stop loss and take profit; conservative stop loss used.",
                    trigger_type="stop_loss",
                    trigger_level=stop,
                    ambiguous_candle=True,
                )
            if stop_hit:
                assert stop is not None
                return _exit_trigger(
                    action=SignalAction.EXIT_LONG,
                    price=stop,
                    reason=_stop_reason(position),
                    trigger_type="stop_loss",
                    trigger_level=stop,
                )
            if target_hit:
                assert target is not None
                return _exit_trigger(
                    action=SignalAction.EXIT_LONG,
                    price=target,
                    reason=_take_profit_reason(position),
                    trigger_type="take_profit",
                    trigger_level=target,
                )
            return None

        stop = position.stop_loss
        target = position.take_profit
        if stop is not None and candle.open >= stop:
            return _exit_trigger(
                action=SignalAction.EXIT_SHORT,
                price=candle.open,
                reason=_gap_stop_reason(position),
                trigger_type="stop_loss",
                trigger_level=stop,
                gap_exit=True,
            )
        if target is not None and candle.open <= target:
            return _exit_trigger(
                action=SignalAction.EXIT_SHORT,
                price=candle.open,
                reason=_gap_take_profit_reason(position),
                trigger_type="take_profit",
                trigger_level=target,
                gap_exit=True,
            )
        stop_hit = stop is not None and candle.high >= stop
        target_hit = target is not None and candle.low <= target
        if stop_hit and target_hit:
            assert stop is not None
            return _exit_trigger(
                action=SignalAction.EXIT_SHORT,
                price=stop,
                reason="Ambiguous candle hit stop loss and take profit; conservative stop loss used.",
                trigger_type="stop_loss",
                trigger_level=stop,
                ambiguous_candle=True,
            )
        if stop_hit:
            assert stop is not None
            return _exit_trigger(
                action=SignalAction.EXIT_SHORT,
                price=stop,
                reason=_stop_reason(position),
                trigger_type="stop_loss",
                trigger_level=stop,
            )
        if target_hit:
            assert target is not None
            return _exit_trigger(
                action=SignalAction.EXIT_SHORT,
                price=target,
                reason=_take_profit_reason(position),
                trigger_type="take_profit",
                trigger_level=target,
            )
        return None

    def _update_trailing_stop(
        self,
        position: PaperPosition,
        candle: OHLCVCandle,
    ) -> None:
        if not self.trailing_stop_enabled:
            return
        if position.stop_loss is None or candle.close <= 0:
            return

        candidate = self._trailing_stop_candidate(position, candle.close)
        if candidate is None:
            return

        if position.direction == SignalDirection.LONG:
            if candidate <= position.stop_loss:
                return
        elif candidate >= position.stop_loss:
            return

        metadata = {
            **position.metadata,
            "trailing_stop_active": True,
            "last_trailing_stop": candidate,
            "last_trailing_reference_price": candle.close,
            "trailing_stop_updated_at_ms": candle.close_time_ms,
            "trailing_stop_update_count": int(
                position.metadata.get("trailing_stop_update_count", 0)
            )
            + 1,
        }
        self.positions[position.pair] = replace(
            position,
            stop_loss=candidate,
            updated_at_ms=candle.close_time_ms,
            metadata=metadata,
        )

    def _trailing_stop_candidate(
        self,
        position: PaperPosition,
        reference_price: Decimal,
    ) -> Decimal | None:
        activation = self.trailing_stop_activation_pct / Decimal("100")
        distance = self.trailing_stop_distance_pct / Decimal("100")

        if position.direction == SignalDirection.LONG:
            activation_price = position.entry_price * (Decimal("1") + activation)
            if reference_price < activation_price:
                return None
            candidate = reference_price * (Decimal("1") - distance)
            if candidate <= 0:
                return None
            return candidate

        activation_price = position.entry_price * (Decimal("1") - activation)
        if reference_price > activation_price:
            return None
        candidate = reference_price * (Decimal("1") + distance)
        if candidate <= 0:
            return None
        return candidate


def _entry_side(action: SignalAction) -> PaperOrderSide | None:
    if action == SignalAction.ENTER_LONG:
        return PaperOrderSide.BUY
    if action == SignalAction.ENTER_SHORT:
        return PaperOrderSide.SELL
    return None


def _exit_side(direction: SignalDirection) -> PaperOrderSide:
    if direction == SignalDirection.LONG:
        return PaperOrderSide.SELL
    return PaperOrderSide.BUY


def _exit_matches_position(action: SignalAction, direction: SignalDirection) -> bool:
    if action == SignalAction.EXIT_LONG:
        return direction == SignalDirection.LONG
    if action == SignalAction.EXIT_SHORT:
        return direction == SignalDirection.SHORT
    return False


def _can_scale_in(signal: StrategySignal, position: PaperPosition) -> bool:
    return (
        bool(signal.metadata.get("allow_scale_in"))
        and signal.direction == position.direction
        and signal.strategy_name == position.strategy_name
    )


def _exit_trigger(
    *,
    action: SignalAction,
    price: Decimal,
    reason: str,
    trigger_type: str,
    trigger_level: Decimal,
    gap_exit: bool = False,
    ambiguous_candle: bool = False,
) -> _ExitTrigger:
    return _ExitTrigger(
        action=action,
        price=price,
        reason=reason,
        metadata={
            "exit_trigger_type": trigger_type,
            "exit_trigger_level": trigger_level,
            "trigger_price_source": "candle_open" if gap_exit else "trigger_level",
            "gap_exit": gap_exit,
            "ambiguous_candle": ambiguous_candle,
        },
    )


def _is_stop_exit(signal: StrategySignal) -> bool:
    trigger_type = str(signal.metadata.get("exit_trigger_type") or "")
    return trigger_type == "stop_loss"


def _slippage_type(signal: StrategySignal) -> str:
    return "stop" if _is_stop_exit(signal) else "normal"


def _exit_fill_metadata(signal: StrategySignal) -> dict[str, object]:
    keys = {
        "exit_trigger_type",
        "exit_trigger_level",
        "trigger_price_source",
        "gap_exit",
        "ambiguous_candle",
    }
    return {key: signal.metadata[key] for key in keys if key in signal.metadata}


def _normalize_fee_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"maker", "taker"}:
        raise ValueError("fee type must be maker or taker.")
    return normalized


def _bool_metadata(
    metadata: Mapping[str, object],
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


def _decimal_metadata(
    metadata: Mapping[str, object],
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


def _stop_reason(position: PaperPosition) -> str:
    if position.metadata.get("atr_dynamic_exit_active") and position.metadata.get("atr_stop_enabled"):
        return "Dynamic ATR stop triggered."
    if position.metadata.get("trailing_stop_active"):
        return "Trailing stop triggered."
    return "Stop loss triggered."


def _take_profit_reason(position: PaperPosition) -> str:
    if position.metadata.get("atr_dynamic_exit_active") and position.metadata.get("atr_take_profit_enabled"):
        return "Dynamic ATR take profit triggered."
    return "Take profit triggered."


def _gap_stop_reason(position: PaperPosition) -> str:
    if position.metadata.get("atr_dynamic_exit_active") and position.metadata.get("atr_stop_enabled"):
        return "Dynamic ATR stop gapped through; filled at candle open."
    if position.metadata.get("trailing_stop_active"):
        return "Trailing stop gapped through; filled at candle open."
    return "Stop loss gapped through; filled at candle open."


def _gap_take_profit_reason(position: PaperPosition) -> str:
    if position.metadata.get("atr_dynamic_exit_active") and position.metadata.get("atr_take_profit_enabled"):
        return "Dynamic ATR take profit gapped through; filled at candle open."
    return "Take profit gapped through; filled at candle open."
