from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping, Any

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
from app.data.indicators import BollingerBandPoint
from app.data.market_events import OrderBookLevel
from app.fees import effective_fee_rate
from app.persistence.paper_state import PaperStateStore
from app.risk.limits import is_entry_signal, is_exit_signal
from app.risk.models import RiskDecision
from app.strategies.base import SignalAction, SignalDirection, StrategySignal
from app.strategies.bollinger_trail_policy import update_bb_trail
from app.utils.time import utc_timestamp_ms


@dataclass(frozen=True)
class _ExitTrigger:
    action: SignalAction
    price: Decimal
    reason: str
    metadata: dict[str, object]


@dataclass(frozen=True)
class _OrderBookState:
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    timestamp_ms: int | None = None


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
        quote_to_margin_rate: Decimal = Decimal("1"),
        unit_contract_value: Decimal = Decimal("1"),
        account_currency: str = "INR",
        price_quote_currency: str = "USDT",
        logger: logging.Logger | None = None,
        state_store: PaperStateStore | None = None,
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
        if quote_to_margin_rate <= 0:
            raise ValueError("quote_to_margin_rate must be positive.")
        if unit_contract_value <= 0:
            raise ValueError("unit_contract_value must be positive.")

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
        self.quote_to_margin_rate = quote_to_margin_rate
        self.unit_contract_value = unit_contract_value
        self.account_currency = account_currency.upper()
        self.price_quote_currency = price_quote_currency.upper()
        self.logger = logger or logging.getLogger(__name__)
        self.state_store = state_store
        
        self.realized_pnl = Decimal("0")
        self.fees_paid = Decimal("0")
        self.funding_paid = Decimal("0")
        
        # Profit Locking Fields
        self.initial_equity = starting_equity
        self.locked_profit = Decimal("0")
        self.unlocked_profit = Decimal("0")
        self.tradable_base = starting_equity
        self.daily_tradable_base_start = starting_equity
        self.daily_loss_from_tradable_base = Decimal("0")
        self.profit_lock_enabled = True
        self.auto_lock_profit_pct = Decimal("100")
        self.protected_profit_override_enabled = False
        self.last_pnl_reset_day = ""

        self.positions: dict[str, PaperPosition] = {}
        self.orders: list[PaperOrder] = []
        self.fills: list[PaperFill] = []
        self._mark_prices: dict[str, Decimal] = {}
        self._orderbooks: dict[str, _OrderBookState] = {}
        self._order_sequence = 0
        self._fill_sequence = 0
        self.restored_state_ignored = False
        self.restored_state_ignored_reason = ""

        if self.state_store:
            saved = self.state_store.load()
            if isinstance(saved, dict) and saved:
                if not _saved_state_currency_compatible(
                    saved,
                    quote_to_margin_rate=self.quote_to_margin_rate,
                    unit_contract_value=self.unit_contract_value,
                ):
                    self.logger.warning(
                        "Ignoring persisted paper state because it was created with "
                        "legacy or different currency conversion settings. Reset the "
                        "paper session to remove old CSV history."
                    )
                    self.restored_state_ignored = True
                    self.restored_state_ignored_reason = "currency_mismatch"
                    return
                # Restore state with proper object reconstruction
                self.positions = {
                    pair: self.state_store._restore_position(pos_dict)
                    for pair, pos_dict in saved["positions"].items()
                }
                self.fills = [
                    self.state_store._restore_fill(f) for f in saved["fills"]
                ]
                self.fees_paid = saved.get("fees_paid", Decimal("0"))
                self.funding_paid = saved.get("funding_paid", Decimal("0"))
                self.realized_pnl = saved.get(
                    "realized_pnl", saved.get("daily_pnl", Decimal("0"))
                )
                
                # Restore Profit Locking Fields
                self.initial_equity = saved.get("initial_equity", self.starting_equity)
                if self.initial_equity is None or self.initial_equity <= 0:
                    self.initial_equity = self.starting_equity
                self.locked_profit = saved.get("locked_profit", Decimal("0"))
                self.unlocked_profit = saved.get("unlocked_profit", Decimal("0"))
                self.tradable_base = saved.get("tradable_base", self.starting_equity)
                if self.tradable_base is None or self.tradable_base <= 0:
                    self.tradable_base = self.starting_equity
                self.daily_tradable_base_start = saved.get("daily_tradable_base_start", self.tradable_base)
                self.daily_loss_from_tradable_base = saved.get("daily_loss_from_tradable_base", Decimal("0"))
                self.profit_lock_enabled = bool(saved.get("profit_lock_enabled", True))
                self.auto_lock_profit_pct = saved.get("auto_lock_profit_pct", Decimal("100"))
                self.protected_profit_override_enabled = bool(saved.get("protected_profit_override_enabled", False))
                self.last_pnl_reset_day = str(saved.get("last_pnl_reset_day", ""))

                self._fill_sequence = max(
                    (_sequence_number(fill.fill_id, "paper-fill-") for fill in self.fills),
                    default=0,
                )
                self._order_sequence = max(
                    (_sequence_number(fill.order_id, "paper-order-") for fill in self.fills),
                    default=0,
                )

                self.logger.info(
                    f"Restored paper state: equity={saved['equity']}, "
                    f"open_positions={len(self.positions)}"
                )

    def _save_state(self) -> None:
        if self.state_store:
            state = {
                "equity": self.snapshot().equity,
                "positions": self.positions,
                "fills": self.fills,
                "realized_pnl": self.realized_pnl,
                "fees_paid": self.fees_paid,
                "funding_paid": self.funding_paid,
                "daily_limit_equity": self.initial_equity,
                "initial_equity": self.initial_equity,
                "locked_profit": self.locked_profit,
                "unlocked_profit": self.unlocked_profit,
                "tradable_base": self.tradable_base,
                "daily_tradable_base_start": self.daily_tradable_base_start,
                "daily_loss_from_tradable_base": self.daily_loss_from_tradable_base,
                "profit_lock_enabled": self.profit_lock_enabled,
                "auto_lock_profit_pct": self.auto_lock_profit_pct,
                "protected_profit_override_enabled": self.protected_profit_override_enabled,
                "last_pnl_reset_day": self.last_pnl_reset_day,
            }
            self.state_store.save(state)

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
        # Reset daily stats if needed
        day = datetime.fromtimestamp(candle.close_time_ms / 1000, tz=timezone.utc).date().isoformat()
        if self.last_pnl_reset_day != day:
            self.daily_loss_from_tradable_base = Decimal("0")
            self.daily_tradable_base_start = self.tradable_base
            self.protected_profit_override_enabled = False
            self.last_pnl_reset_day = day
            self._save_state()

        reports: list[PaperExecutionReport] = []
        position = self.positions.get(candle.pair)
        if position is None:
            return reports

        trigger = self._trigger_for_position(position, candle)
        if trigger is None:
            self._update_trailing_stop(position, candle)
            self._save_state()
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
        if trigger.metadata.get("partial_close"):
            reports.append(
                self._partial_close_from_signal(
                    signal,
                    market_price=trigger.price,
                    timestamp_ms=candle.close_time_ms,
                    risk_decision=None,
                    reason=trigger.reason,
                )
            )
        else:
            reports.append(
                self._close_from_signal(
                    signal,
                    market_price=trigger.price,
                    timestamp_ms=candle.close_time_ms,
                    risk_decision=None,
                    reason=trigger.reason,
                )
            )
        self._save_state()
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
            notional = position.margin_notional(mark_price)
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
        trailing_multiple: Decimal | None = None,
        stop_enabled: bool = True,
        take_profit_enabled: bool = True,
        trailing_enabled: bool = True,
        take_profit_mode: str = "fixed",
        breakeven_enabled: bool = False,
        breakeven_activation_r: Decimal = Decimal("1.0"),
        breakeven_offset_r: Decimal = Decimal("0"),
        profit_lock_enabled: bool = False,
        profit_lock_activation_r: Decimal = Decimal("1.5"),
        profit_lock_r: Decimal = Decimal("0.5"),
        atr_trail_after_r_enabled: bool = False,
        atr_trail_activation_r: Decimal = Decimal("2.0"),
        bb_band: BollingerBandPoint | None = None,
        bb_trail_enabled: bool = False,
        bb_trail_buffer_multiplier: Decimal = Decimal("1.0"),
        bb_trail_activation_r: Decimal = Decimal("0.5"),
        bb_trail_stage2_r: Decimal = Decimal("0.5"),
        bb_trail_stage3_r: Decimal = Decimal("1.0"),
        bb_trail_force_close_r: Decimal = Decimal("4.0"),
        bb_trail_partial_close_at_tp: bool = True,
        bb_trail_partial_close_pct: Decimal = Decimal("0.60"),
    ) -> None:
        if atr is None or atr <= 0:
            return

        position = self.positions.get(candle.pair)
        if position is None:
            return
        if not _dynamic_atr_exits_enabled(position) and not _bool_metadata(
            position.metadata,
            "bb_trail_enabled",
            bb_trail_enabled,
        ):
            return
        if position.opened_at_ms > candle.close_time_ms:
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
        
        # Task 5 config extraction
        breakeven_enabled = _bool_metadata(position.metadata, "breakeven_enabled", breakeven_enabled)
        profit_lock_enabled = _bool_metadata(position.metadata, "profit_lock_enabled", profit_lock_enabled)
        atr_trail_after_r_enabled = _bool_metadata(position.metadata, "atr_trail_after_r_enabled", atr_trail_after_r_enabled)
        adaptive_stop_management_enabled = _bool_metadata(
            position.metadata,
            "adaptive_stop_management_enabled",
            False,
        )
        breakeven_activation_r = _decimal_metadata(
            position.metadata,
            "breakeven_activation_r",
            breakeven_activation_r,
        )
        breakeven_offset_r = _decimal_metadata(
            position.metadata,
            "breakeven_offset_r",
            breakeven_offset_r,
        )
        profit_lock_activation_r = _decimal_metadata(
            position.metadata,
            "profit_lock_activation_r",
            profit_lock_activation_r,
        )
        profit_lock_r = _decimal_metadata(
            position.metadata,
            "profit_lock_r",
            profit_lock_r,
        )
        atr_trail_activation_r = _decimal_metadata(
            position.metadata,
            "atr_trail_activation_r",
            atr_trail_activation_r,
        )
        bb_trail_enabled = _bool_metadata(
            position.metadata,
            "bb_trail_enabled",
            bb_trail_enabled,
        )
        bb_trail_buffer_multiplier = _decimal_metadata(
            position.metadata,
            "bb_trail_buffer_multiplier",
            bb_trail_buffer_multiplier,
        )
        bb_trail_activation_r = _decimal_metadata(
            position.metadata,
            "bb_trail_activation_r",
            bb_trail_activation_r,
        )
        bb_trail_stage2_r = _decimal_metadata(
            position.metadata,
            "bb_trail_stage2_r",
            bb_trail_stage2_r,
        )
        bb_trail_stage3_r = _decimal_metadata(
            position.metadata,
            "bb_trail_stage3_r",
            bb_trail_stage3_r,
        )
        bb_trail_force_close_r = _decimal_metadata(
            position.metadata,
            "bb_trail_force_close_r",
            bb_trail_force_close_r,
        )
        bb_trail_partial_close_at_tp = _bool_metadata(
            position.metadata,
            "bb_trail_partial_close_at_tp",
            bb_trail_partial_close_at_tp,
        )
        bb_trail_partial_close_pct = _decimal_metadata(
            position.metadata,
            "bb_trail_partial_close_pct",
            bb_trail_partial_close_pct,
        )

        if (
            not stop_enabled
            and not take_profit_enabled
            and not breakeven_enabled
            and not profit_lock_enabled
            and not bb_trail_enabled
        ):
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
                trailing_multiple if trailing_multiple is not None else stop_multiple,
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
        if (
            not stop_enabled
            and not take_profit_enabled
            and not breakeven_enabled
            and not profit_lock_enabled
            and not bb_trail_enabled
        ):
            return

        # Entry ATR Cap: trailing_distance = min(current_atr, entry_atr) * trailing_multiple
        entry_atr = _decimal_metadata(position.metadata, "atr_entry_atr", atr)
        trailing_effective_atr = min(atr, entry_atr) if entry_atr > 0 else atr
        
        stop_distance = atr * stop_multiple
        trailing_distance = trailing_effective_atr * trailing_multiple
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

        # Task 5 R-based management
        initial_stop = _decimal_metadata(position.metadata, "initial_stop_loss", None)
        r_unit = abs(position.entry_price - initial_stop) if (initial_stop is not None and initial_stop > 0) else None
        current_profit = (candle.close - position.entry_price) if position.direction == SignalDirection.LONG else (position.entry_price - candle.close)
        current_r = (current_profit / r_unit) if (r_unit is not None and r_unit > 0) else Decimal("0")
        adaptive_settings: dict[str, Decimal | str] = {}
        if adaptive_stop_management_enabled and r_unit is not None and r_unit > 0:
            adaptive_settings = _adaptive_stop_management_settings(
                position=position,
                current_atr=atr,
                entry_atr=entry_atr,
                r_unit=r_unit,
                trailing_distance=trailing_distance,
            )
            breakeven_activation_r = adaptive_settings["breakeven_activation_r"]  # type: ignore[assignment]
            profit_lock_activation_r = adaptive_settings["profit_lock_activation_r"]  # type: ignore[assignment]
            profit_lock_r = adaptive_settings["profit_lock_r"]  # type: ignore[assignment]
        
        management_stop = None
        stop_type = "atr"
        management_cost_buffer = self._stop_management_cost_buffer(position)
        if breakeven_enabled and current_r >= breakeven_activation_r:
            offset = breakeven_offset_r * r_unit if r_unit else Decimal("0")
            offset = max(offset, management_cost_buffer)
            if current_profit <= offset:
                offset = None
        else:
            offset = None
        if offset is not None:
            if position.direction == SignalDirection.LONG:
                management_stop = position.entry_price + offset
            else:
                management_stop = position.entry_price - offset
            stop_type = "breakeven"
                
        if profit_lock_enabled and current_r >= profit_lock_activation_r:
            lock_offset = profit_lock_r * r_unit if r_unit else Decimal("0")
            lock_offset = max(lock_offset, management_cost_buffer)
            if current_profit <= lock_offset:
                lock_offset = None
        else:
            lock_offset = None
        if lock_offset is not None:
            if position.direction == SignalDirection.LONG:
                candidate = position.entry_price + lock_offset
                if management_stop is None or candidate > management_stop:
                    management_stop = candidate
                    stop_type = "profit_lock"
            else:
                candidate = position.entry_price - lock_offset
                if management_stop is None or candidate < management_stop:
                    management_stop = candidate
                    stop_type = "profit_lock"

        # ATR Trail after R activation
        actual_trailing_enabled = trailing_enabled
        if atr_trail_after_r_enabled:
            actual_trailing_enabled = (current_r >= atr_trail_activation_r)

        if position.direction == SignalDirection.LONG:
            if actual_trailing_enabled:
                best_price = max(
                    _decimal_metadata(position.metadata, "atr_best_price", position.entry_price),
                    candle.high,
                    position.entry_price,
                )
            else:
                best_price = position.entry_price
            stop_candidate = position.stop_loss
            if stop_enabled or management_stop:
                distance = trailing_distance if actual_trailing_enabled else stop_distance
                # Combine ATR stop and Management stop
                atr_stop = max(best_price - distance, tiny_price) if stop_enabled else None
                
                if atr_stop and management_stop:
                    stop_candidate = max(atr_stop, management_stop)
                elif atr_stop:
                    stop_candidate = atr_stop
                else:
                    stop_candidate = management_stop
                
                if position.stop_loss is not None:
                    stop_candidate = max(position.stop_loss, stop_candidate)
            entry_target = max(position.entry_price + target_distance, tiny_price)
            if take_profit_mode == "none":
                take_profit = position.take_profit if position.metadata.get("manual_take_profit_pct") else None
            elif take_profit_mode in {"fixed", "trailing_atr"}:
                take_profit = position.take_profit
            elif take_profit_mode == "entry_atr":
                take_profit = entry_target
            elif take_profit_mode == "ratchet":
                ratchet_target = max(best_price + target_distance, tiny_price)
                take_profit = max(position.take_profit or entry_target, ratchet_target)
            else:
                # Default behavior: trail using current close, but never loosen (move down for long)
                candidate = max(candle.close + target_distance, tiny_price)
                take_profit = max(position.take_profit or candidate, candidate)
        else:
            if actual_trailing_enabled:
                best_price = min(
                    _decimal_metadata(position.metadata, "atr_best_price", position.entry_price),
                    candle.low,
                    position.entry_price,
                )
            else:
                best_price = position.entry_price
            stop_candidate = position.stop_loss
            if stop_enabled or management_stop:
                distance = trailing_distance if actual_trailing_enabled else stop_distance
                atr_stop = best_price + distance if stop_enabled else None
                
                if atr_stop and management_stop:
                    stop_candidate = min(atr_stop, management_stop)
                elif atr_stop:
                    stop_candidate = atr_stop
                else:
                    stop_candidate = management_stop
                    
                if position.stop_loss is not None:
                    stop_candidate = min(position.stop_loss, stop_candidate)
            entry_target = max(position.entry_price - target_distance, tiny_price)
            if take_profit_mode == "none":
                take_profit = position.take_profit if position.metadata.get("manual_take_profit_pct") else None
            elif take_profit_mode in {"fixed", "trailing_atr"}:
                take_profit = position.take_profit
            elif take_profit_mode == "entry_atr":
                take_profit = entry_target
            elif take_profit_mode == "ratchet":
                ratchet_target = max(best_price - target_distance, tiny_price)
                take_profit = min(position.take_profit or entry_target, ratchet_target)
            else:
                # Default behavior: trail using current close, but never loosen (move up for short)
                candidate = max(candle.close - target_distance, tiny_price)
                take_profit = min(position.take_profit or candidate, candidate)

        bb_metadata: dict[str, object] = {}
        bb_stop_applied = False
        if bb_trail_enabled and bb_band is not None:
            position_for_bb = replace(
                position,
                metadata={**position.metadata, "atr_best_price": best_price},
            )
            bb_metadata = update_bb_trail(
                position=position_for_bb,
                current_r=current_r,
                middle_band=bb_band.middle,
                upper_band=bb_band.upper,
                lower_band=bb_band.lower,
                atr=atr,
                buffer_multiplier=bb_trail_buffer_multiplier,
                activation_r=bb_trail_activation_r,
                stage2_r=bb_trail_stage2_r,
                stage3_r=bb_trail_stage3_r,
                force_close_r=bb_trail_force_close_r,
            )
            bb_stop = bb_metadata.get("bb_trail_stop")
            if bb_metadata.get("bb_trail_active") and isinstance(bb_stop, Decimal):
                if position.direction == SignalDirection.LONG:
                    combined_stop = max(stop_candidate or Decimal("0"), bb_stop)
                else:
                    combined_stop = min(
                        stop_candidate or Decimal("999999999"),
                        bb_stop,
                    )
                bb_stop_applied = combined_stop == bb_stop
                stop_candidate = combined_stop

        resolved_stop_type = (
            stop_type
            if management_stop is not None and stop_candidate == management_stop
            else (
                position.metadata.get("stop_type")
                if stop_candidate == position.stop_loss
                and position.metadata.get("stop_type") in {"breakeven", "profit_lock"}
                else "atr"
            )
        )
        if bb_stop_applied:
            resolved_stop_type = "bb_trail"

        bb_partial_enabled = (
            bb_trail_enabled
            and bb_trail_partial_close_at_tp
            and bool(bb_metadata.get("bb_trail_active", False))
            and Decimal("0") < bb_trail_partial_close_pct < Decimal("1")
        )

        metadata = {
            **position.metadata,
            "atr_dynamic_exit_active": True,
            "atr_dynamic_exits_enabled": True,
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
            "take_profit_suppressed_by_trailing": _trailing_priority_enabled(
                replace(
                    position,
                    metadata={
                        **position.metadata,
                        "atr_dynamic_exits_enabled": True,
                        "atr_trailing_enabled": trailing_enabled,
                        "profit_lock_enabled": profit_lock_enabled,
                        "bb_trail_enabled": bb_trail_enabled,
                        "bb_trail_active": bool(bb_metadata.get("bb_trail_active", False)),
                    },
                )
            ),
            "atr_exit_updated_at_ms": candle.close_time_ms,
            "adaptive_stop_management_enabled": adaptive_stop_management_enabled,
            "atr_exit_update_count": int(
                position.metadata.get("atr_exit_update_count", 0)
            )
            + 1,
            "stop_type": resolved_stop_type,
        }
        metadata.update(bb_metadata)
        metadata["stop_type"] = resolved_stop_type
        if bb_partial_enabled:
            metadata.update(
                {
                    "tp_is_partial_close": True,
                    "tp_partial_close_pct": bb_trail_partial_close_pct,
                    "bb_trail_partial_close_pct": bb_trail_partial_close_pct,
                    "bb_trail_partial_close_at_tp": bb_trail_partial_close_at_tp,
                    "take_profit_suppressed_by_bb_trail": True,
                }
            )
        if adaptive_settings:
            metadata.update(adaptive_settings)
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
        # ... (implementation remains same as updated)
        # (I will provide the full block to be safe)
        if mark_prices:
            self.update_mark_prices(mark_prices)

        unrealized = Decimal("0")
        for pair, position in self.positions.items():
            mark_price = self._mark_prices.get(pair)
            if mark_price is not None:
                unrealized += position.unrealized_pnl(mark_price)

        open_notional = Decimal("0")
        for pair, position in self.positions.items():
            mark_price = self._mark_prices.get(pair)
            price = mark_price if mark_price is not None else position.entry_price
            open_notional += position.margin_notional(price)

        initial_equity = (
            self.initial_equity
            if self.initial_equity is not None and self.initial_equity > 0
            else self.starting_equity
        )
        tradable_base = (
            self.tradable_base
            if self.tradable_base is not None and self.tradable_base > 0
            else self.starting_equity
        )

        total_equity = (
            self.starting_equity
            + self.realized_pnl
            + unrealized
            - self.fees_paid
            - self.funding_paid
        )
        
        tradable_equity = total_equity - self.locked_profit

        return PaperAccountSnapshot(
            starting_equity=self.starting_equity,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            fees_paid=self.fees_paid,
            equity=total_equity,
            open_position_count=len(self.positions),
            open_notional=open_notional,
            funding_paid=self.funding_paid,
            initial_equity=initial_equity,
            total_equity=total_equity,
            tradable_equity=tradable_equity,
            tradable_base=self.tradable_base,
            daily_tradable_base_start=self.daily_tradable_base_start,
            locked_profit=self.locked_profit,

            unlocked_profit=self.unlocked_profit,
            session_profit=self.realized_pnl - self.fees_paid,
            daily_loss_from_tradable_base=self.daily_loss_from_tradable_base,
            profit_lock_enabled=self.profit_lock_enabled,
            auto_lock_profit_pct=self.auto_lock_profit_pct,
            protected_profit_override_enabled=self.protected_profit_override_enabled,
        )

    def lock_profit(self, amount: Decimal, reason: str = "") -> None:
        if amount <= 0: return
        actual_amount = min(amount, self.tradable_base)
        self.tradable_base -= actual_amount
        self.locked_profit += actual_amount
        self.logger.info(f"Manually locked {actual_amount} profit. Reason: {reason}")
        self._save_state()

    def unlock_profit(self, amount: Decimal, reason: str = "") -> None:
        if amount <= 0: return
        actual_amount = min(amount, self.locked_profit)
        self.locked_profit -= actual_amount
        self.tradable_base += actual_amount
        self.unlocked_profit += actual_amount
        self.logger.info(f"Manually unlocked {actual_amount} profit. Reason: {reason}")
        self._save_state()


    def open_positions(self) -> list[PaperPosition]:
        return list(self.positions.values())

    def update_mark_prices(self, mark_prices: Mapping[str, Decimal]) -> None:
        for pair, price in mark_prices.items():
            if price is None:
                continue
            self._mark_prices[pair] = Decimal(str(price))

    def mark_price_for(self, pair: str, fallback: Decimal | None = None) -> Decimal | None:
        return self._mark_prices.get(pair, fallback)

    def margin_notional(self, quantity: Decimal, price: Decimal) -> Decimal:
        return abs(
            quantity
            * price
            * self.unit_contract_value
            * self.quote_to_margin_rate
        )

    def update_orderbook(
        self,
        pair: str,
        *,
        bids: list[OrderBookLevel],
        asks: list[OrderBookLevel],
        timestamp_ms: int | None = None,
    ) -> None:
        clean_bids = sorted(
            [level for level in bids if level.price > 0 and level.quantity > 0],
            key=lambda level: level.price,
            reverse=True,
        )
        clean_asks = sorted(
            [level for level in asks if level.price > 0 and level.quantity > 0],
            key=lambda level: level.price,
        )
        if not clean_bids and not clean_asks:
            return
        self._orderbooks[pair] = _OrderBookState(
            bids=clean_bids,
            asks=clean_asks,
            timestamp_ms=timestamp_ms,
        )

    def orderbook_summary(self, pair: str, *, depth: int = 5) -> dict[str, Decimal | int | None]:
        book = self._orderbooks.get(pair)
        if book is None:
            return {}
        depth = max(depth, 1)
        best_bid = book.bids[0].price if book.bids else None
        best_ask = book.asks[0].price if book.asks else None
        bid_depth_quote = sum(
            (level.price * level.quantity for level in book.bids[:depth]),
            Decimal("0"),
        )
        ask_depth_quote = sum(
            (level.price * level.quantity for level in book.asks[:depth]),
            Decimal("0"),
        )
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_pct": _spread_pct(best_bid, best_ask),
            "bid_depth_quote": bid_depth_quote,
            "ask_depth_quote": ask_depth_quote,
            "timestamp_ms": book.timestamp_ms,
        }

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

        leverage = decision.leverage or Decimal("1")
        quantity = decision.position_size
        fill_price, slippage_details = self._paper_fill_price(
            pair=signal.pair,
            side=side,
            quantity=quantity,
            base_price=base_price,
            fallback_slippage_pct=self.slippage_pct,
            slippage_type="entry",
        )
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
        fee_notional = self.margin_notional(order.quantity, order.price)
        fee_details = self._fee_details(fee_notional, fee_type)
        fee = fee_details["total_fee"]
        fill = self._fill(
            order=order,
            fee=fee,
            timestamp_ms=timestamp_ms,
            metadata={
                **fee_details,
                **slippage_details,
                "notional_quote": order.notional,
                "notional_margin": fee_notional,
                "account_currency": self.account_currency,
                "price_quote_currency": self.price_quote_currency,
                "quote_to_margin_rate": self.quote_to_margin_rate,
                "unit_contract_value": self.unit_contract_value,
                "risk_percent_used": decision.metadata.get("risk_percent_used"),
                "risk_multiplier": decision.metadata.get("risk_multiplier"),
                "risk_base_mode": decision.metadata.get("risk_base_mode"),
                "risk_base_amount": decision.metadata.get("risk_base_amount"),
                "planned_risk_amount": decision.metadata.get("planned_risk_amount"),
                "required_margin": margin_metadata.get("required_margin"),
                "position_notional": margin_metadata.get("position_notional"),
                "position_notional_margin": margin_metadata.get("position_notional_margin"),
                "position_notional_quote": margin_metadata.get("position_notional_quote"),
            },
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
            quote_to_margin_rate=self.quote_to_margin_rate,
            unit_contract_value=self.unit_contract_value,
            metadata={
                **decision.metadata,
                **signal.metadata,
                "risk_max_loss": decision.max_loss,
                "initial_stop_loss": signal.stop_loss,
                **margin_metadata,
                "trailing_stop_enabled": _bool_metadata(
                    signal.metadata,
                    "trailing_stop_enabled",
                    self.trailing_stop_enabled,
                ),
                "trailing_stop_activation_pct": _decimal_metadata(
                    signal.metadata,
                    "trailing_stop_activation_pct",
                    self.trailing_stop_activation_pct,
                ),
                "trailing_stop_distance_pct": _decimal_metadata(
                    signal.metadata,
                    "trailing_stop_distance_pct",
                    self.trailing_stop_distance_pct,
                ),
                "account_equity_at_entry": self.snapshot().tradable_equity,
                "entry_fee_type": fee_type,
                "entry_fee": fee,
                "entry_fee_rate": fee_details["fee_rate"],
                "entry_fee_gst_rate": self.fee_gst_rate,
                "entry_effective_fee_rate": fee_details["effective_fee_rate"],
                "entry_fees_total": fee,
                "notional_quote": order.notional,
                "notional_margin": fee_notional,
                "account_currency": self.account_currency,
                "price_quote_currency": self.price_quote_currency,
                "quote_to_margin_rate": self.quote_to_margin_rate,
                "unit_contract_value": self.unit_contract_value,
                "quantity_unit": _quantity_unit(signal.pair),
                "entry_slippage_model": slippage_details["slippage_model"],
                "entry_slippage_pct": slippage_details["slippage_pct"],
            },
        )
        self.positions[signal.pair] = position
        self.orders.append(order)
        self.fills.append(fill)
        self._save_state()

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

        leverage = decision.leverage or position.leverage
        quantity = decision.position_size
        fill_price, slippage_details = self._paper_fill_price(
            pair=signal.pair,
            side=side,
            quantity=quantity,
            base_price=base_price,
            fallback_slippage_pct=self.slippage_pct,
            slippage_type="entry",
        )
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
        fee_notional = self.margin_notional(order.quantity, order.price)
        fee_details = self._fee_details(fee_notional, fee_type)
        fee = fee_details["total_fee"]
        fill = self._fill(
            order=order,
            fee=fee,
            timestamp_ms=timestamp_ms,
            metadata={
                **fee_details,
                **slippage_details,
                "notional_quote": order.notional,
                "notional_margin": fee_notional,
                "account_currency": self.account_currency,
                "price_quote_currency": self.price_quote_currency,
                "quote_to_margin_rate": position.quote_to_margin_rate,
                "unit_contract_value": position.unit_contract_value,
                "risk_percent_used": position.metadata.get("risk_percent_used"),
                "risk_multiplier": position.metadata.get("risk_multiplier"),
                "risk_base_mode": position.metadata.get("risk_base_mode"),
                "risk_base_amount": position.metadata.get("risk_base_amount"),
                "planned_risk_amount": position.metadata.get("planned_risk_amount"),
                "required_margin": position.metadata.get("required_margin"),
                "position_notional": position.metadata.get("position_notional"),
                "position_notional_margin": position.metadata.get("position_notional_margin"),
                "position_notional_quote": position.metadata.get("position_notional_quote"),
            },
        )
        self.fees_paid += fee

        new_quantity = position.quantity + quantity
        average_entry = (
            (position.quantity * position.entry_price) + (quantity * fill_price)
        ) / new_quantity
        existing_entry_fees = _decimal_metadata(
            position.metadata,
            "entry_fees_total",
            _decimal_metadata(position.metadata, "entry_fee", Decimal("0")),
        )
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
            "entry_fees_total": existing_entry_fees + fee,
            "last_scale_in_fee_rate": fee_details["fee_rate"],
            "last_scale_in_fee_gst_rate": self.fee_gst_rate,
            "last_scale_in_effective_fee_rate": fee_details["effective_fee_rate"],
            "last_scale_in_notional_quote": order.notional,
            "last_scale_in_notional_margin": fee_notional,
            "last_scale_in_slippage_model": slippage_details["slippage_model"],
            "last_scale_in_slippage_pct": slippage_details["slippage_pct"],
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
        self._save_state()

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
        fill_price, slippage_details = self._paper_fill_price(
            pair=position.pair,
            side=side,
            quantity=position.quantity,
            base_price=base_price,
            fallback_slippage_pct=slippage_pct,
            slippage_type=_slippage_type(signal),
        )
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
        fee_notional = position.margin_notional(order.price)
        fee_details = self._fee_details(fee_notional, fee_type)
        fee = fee_details["total_fee"]
        fill = self._fill(
            order=order,
            fee=fee,
            timestamp_ms=timestamp_ms,
            realized_pnl=gross_pnl,
            metadata={
                **fee_details,
                **slippage_details,
                **_exit_fill_metadata(signal),
                "notional_quote": order.notional,
                "notional_margin": fee_notional,
                "account_currency": self.account_currency,
                "price_quote_currency": self.price_quote_currency,
                "quote_to_margin_rate": self.quote_to_margin_rate,
                "unit_contract_value": self.unit_contract_value,
                "account_equity_at_exit": self.snapshot().equity,
                },
                )

        self.realized_pnl += gross_pnl
        self.fees_paid += fee
        
        # Profit Locking Logic
        entry_fee = _decimal_metadata(
            position.metadata,
            "entry_fees_total",
            _decimal_metadata(position.metadata, "entry_fee", Decimal("0")),
        )
        net_trade_pnl = gross_pnl - fee - entry_fee
        
        if net_trade_pnl > 0 and self.profit_lock_enabled:
            lock_amount = net_trade_pnl * (self.auto_lock_profit_pct / Decimal("100"))
            self.locked_profit += lock_amount
            # Tradable base DOES NOT increase automatically from profits
            # Unlocked profit part (if auto_lock < 100) will be reflected in tradable_equity 
            # (which is total_equity - locked_profit) but NOT in tradable_base.
        elif net_trade_pnl < 0:
            # Losses always reduce tradable base
            self.tradable_base += net_trade_pnl
            # We track daily loss from tradable base for the kill switch
            self.daily_loss_from_tradable_base += abs(net_trade_pnl)

        closed_position = self.positions.pop(position.pair)
        self.orders.append(order)
        self.fills.append(fill)
        self._save_state()

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

    def _partial_close_from_signal(
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
                reason="Partial exit signal direction does not match the open position.",
                timestamp_ms=timestamp_ms,
                risk_decision=risk_decision,
                signal=signal,
                market_price=market_price,
            )

        partial_pct = _decimal_metadata(
            signal.metadata,
            "partial_close_pct",
            _decimal_metadata(position.metadata, "bb_trail_partial_close_pct", Decimal("0.60")),
        )
        if partial_pct <= 0 or partial_pct >= 1:
            return self._rejected_report(
                reason="Partial close percentage must be between 0 and 1.",
                timestamp_ms=timestamp_ms,
                risk_decision=risk_decision,
                signal=signal,
                market_price=market_price,
            )

        base_price = market_price or signal.entry_price
        if base_price is None or base_price <= 0:
            return self._rejected_report(
                reason="A positive market price or signal price is required to partially close.",
                timestamp_ms=timestamp_ms,
                risk_decision=risk_decision,
                signal=signal,
                market_price=market_price,
            )

        partial_quantity = position.quantity * partial_pct
        if partial_quantity <= 0 or partial_quantity >= position.quantity:
            return self._rejected_report(
                reason="Partial close quantity must leave a remaining position.",
                timestamp_ms=timestamp_ms,
                risk_decision=risk_decision,
                signal=signal,
                market_price=market_price,
            )

        side = _exit_side(position.direction)
        slippage_pct = self._exit_slippage_pct(signal)
        fill_price, slippage_details = self._paper_fill_price(
            pair=position.pair,
            side=side,
            quantity=partial_quantity,
            base_price=base_price,
            fallback_slippage_pct=slippage_pct,
            slippage_type=_slippage_type(signal),
        )
        action = (
            SignalAction.EXIT_LONG
            if position.direction == SignalDirection.LONG
            else SignalAction.EXIT_SHORT
        )
        order = self._filled_order(
            pair=position.pair,
            side=side,
            action=action,
            quantity=partial_quantity,
            price=fill_price,
            leverage=position.leverage,
            timestamp_ms=timestamp_ms,
            reason=reason,
        )
        closed_position = replace(position, quantity=partial_quantity)
        gross_pnl = closed_position.unrealized_pnl(fill_price)
        fee_type = self._fee_type_for_signal(signal, default_fee_type=self.exit_fee_type)
        fee_notional = self.margin_notional(order.quantity, order.price)
        fee_details = self._fee_details(fee_notional, fee_type)
        fee = fee_details["total_fee"]
        total_entry_fee = _decimal_metadata(
            position.metadata,
            "entry_fees_total",
            _decimal_metadata(position.metadata, "entry_fee", Decimal("0")),
        )
        entry_fee_allocated = total_entry_fee * partial_pct
        remaining_entry_fee = max(total_entry_fee - entry_fee_allocated, Decimal("0"))
        closed_metadata = {
            **position.metadata,
            "entry_fee": entry_fee_allocated,
            "entry_fees_total": entry_fee_allocated,
            "partial_close": True,
            "partial_close_pct": partial_pct,
            "bb_partial_close_executed": True,
        }
        closed_position = replace(closed_position, metadata=closed_metadata)
        fill = self._fill(
            order=order,
            fee=fee,
            timestamp_ms=timestamp_ms,
            realized_pnl=gross_pnl,
            metadata={
                **fee_details,
                **slippage_details,
                **_exit_fill_metadata(signal),
                "partial_close": True,
                "partial_close_pct": partial_pct,
                "entry_fee_allocated": entry_fee_allocated,
                "notional_quote": order.notional,
                "notional_margin": fee_notional,
                "account_currency": self.account_currency,
                "price_quote_currency": self.price_quote_currency,
                "quote_to_margin_rate": self.quote_to_margin_rate,
                "unit_contract_value": self.unit_contract_value,
                "account_equity_at_exit": self.snapshot().equity,
            },
        )

        self.realized_pnl += gross_pnl
        self.fees_paid += fee

        net_trade_pnl = gross_pnl - fee - entry_fee_allocated
        if net_trade_pnl > 0 and self.profit_lock_enabled:
            lock_amount = net_trade_pnl * (self.auto_lock_profit_pct / Decimal("100"))
            self.locked_profit += lock_amount
        elif net_trade_pnl < 0:
            self.tradable_base += net_trade_pnl
            self.daily_loss_from_tradable_base += abs(net_trade_pnl)

        remaining_quantity = position.quantity - partial_quantity
        remaining_ratio = remaining_quantity / position.quantity
        remaining_metadata = {
            **position.metadata,
            "entry_fee": remaining_entry_fee,
            "entry_fees_total": remaining_entry_fee,
            "bb_partial_close_executed": True,
            "last_partial_close_price": fill_price,
            "last_partial_close_qty": partial_quantity,
            "last_partial_close_pct": partial_pct,
            "last_partial_close_at_ms": timestamp_ms,
        }
        for key in (
            "required_margin",
            "position_notional",
            "position_notional_margin",
            "position_notional_quote",
            "planned_risk_amount",
            "risk_max_loss",
        ):
            value = _decimal_metadata(remaining_metadata, key, None)
            if value is not None:
                remaining_metadata[key] = value * remaining_ratio

        self.positions[position.pair] = replace(
            position,
            quantity=remaining_quantity,
            updated_at_ms=timestamp_ms,
            metadata=remaining_metadata,
        )
        self.orders.append(order)
        self.fills.append(fill)
        self._save_state()

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
            metadata={"partial_closed_position": True},
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
        notional = self.margin_notional(quantity, price)
        notional_quote = abs(quantity * price)
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
            "leverage": leverage,
            "planned_risk_amount": planned_risk,
            "position_quantity": quantity,
            "position_notional": notional,
            "position_notional_quote": notional_quote,
            "position_notional_margin": notional,
            "max_position_notional": max_notional,
            "margin_ok": margin_ok,
            "account_blown": account_blown,
            "account_currency": self.account_currency,
            "price_quote_currency": self.price_quote_currency,
            "quote_to_margin_rate": self.quote_to_margin_rate,
            "unit_contract_value": self.unit_contract_value,
            "risk_base_amount": decision.metadata.get("risk_base_amount"),
            "risk_base_mode": decision.metadata.get("risk_base_mode"),
            "risk_multiplier": decision.metadata.get("risk_multiplier"),
            "risk_percent_used": decision.metadata.get("risk_percent_used"),
        }
        
        if account_blown:
            return "Paper broker rejected entry: account equity is depleted (bankrupt).", metadata
        if snapshot.equity < required_margin:
            return (
                f"Paper broker rejected entry: required margin {required_margin} exceeds available equity {snapshot.equity}.",
                metadata,
            )
        if snapshot.equity < planned_risk:
            return (
                f"Paper broker rejected entry: planned risk {planned_risk} exceeds available equity {snapshot.equity}.",
                metadata,
            )
        if notional > max_notional:
            return (
                f"Paper broker rejected entry: position notional {notional} exceeds equity times leverage {max_notional}.",
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

    def _paper_fill_price(
        self,
        *,
        pair: str,
        side: PaperOrderSide,
        quantity: Decimal,
        base_price: Decimal,
        fallback_slippage_pct: Decimal,
        slippage_type: str,
    ) -> tuple[Decimal, dict[str, object]]:
        book = self._orderbooks.get(pair)
        fixed_price = self._apply_slippage(base_price, side, fallback_slippage_pct)
        if book is None or quantity <= 0:
            return fixed_price, {
                "slippage_model": "fixed_pct",
                "pre_slippage_price": base_price,
                "slippage_pct": fallback_slippage_pct,
                "configured_slippage_pct": fallback_slippage_pct,
                "slippage_type": slippage_type,
            }

        levels = book.asks if side == PaperOrderSide.BUY else book.bids
        if not levels:
            return fixed_price, {
                "slippage_model": "fixed_pct_no_book_side",
                "pre_slippage_price": base_price,
                "slippage_pct": fallback_slippage_pct,
                "configured_slippage_pct": fallback_slippage_pct,
                "slippage_type": slippage_type,
                "orderbook_timestamp_ms": book.timestamp_ms,
            }

        remaining = quantity
        visible_filled = Decimal("0")
        notional = Decimal("0")
        for level in levels:
            if remaining <= 0:
                break
            take = min(remaining, level.quantity)
            notional += take * level.price
            visible_filled += take
            remaining -= take

        if visible_filled <= 0:
            return fixed_price, {
                "slippage_model": "fixed_pct_empty_book",
                "pre_slippage_price": base_price,
                "slippage_pct": fallback_slippage_pct,
                "configured_slippage_pct": fallback_slippage_pct,
                "slippage_type": slippage_type,
                "orderbook_timestamp_ms": book.timestamp_ms,
            }

        if remaining > 0:
            # We only subscribe to finite visible depth. Price the unfilled tail with
            # the fixed fallback so the simulation remains conservative and complete.
            notional += remaining * fixed_price

        fill_price = notional / quantity
        slippage_pct = _slippage_pct_from_fill(
            base_price=base_price,
            fill_price=fill_price,
            side=side,
        )
        best_bid = book.bids[0].price if book.bids else None
        best_ask = book.asks[0].price if book.asks else None
        return fill_price, {
            "slippage_model": "live_orderbook" if remaining <= 0 else "live_orderbook_plus_fixed_tail",
            "pre_slippage_price": base_price,
            "slippage_pct": slippage_pct,
            "configured_slippage_pct": fallback_slippage_pct,
            "slippage_type": slippage_type,
            "orderbook_timestamp_ms": book.timestamp_ms,
            "orderbook_side": "asks" if side == PaperOrderSide.BUY else "bids",
            "orderbook_requested_quantity": quantity,
            "orderbook_visible_filled_quantity": visible_filled,
            "orderbook_depth_exhausted": remaining > 0,
            "orderbook_best_bid": best_bid,
            "orderbook_best_ask": best_ask,
            "orderbook_spread_pct": _spread_pct(best_bid, best_ask),
        }

    def _exit_slippage_pct(self, signal: StrategySignal) -> Decimal:
        if _is_stop_exit(signal) and self.stop_slippage_pct is not None:
            return self.stop_slippage_pct
        return self.slippage_pct

    def _stop_management_cost_buffer(self, position: PaperPosition) -> Decimal:
        entry_rate = _decimal_metadata(
            position.metadata,
            "entry_effective_fee_rate",
            effective_fee_rate(self._fee_rate(self.entry_fee_type), self.fee_gst_rate),
        )
        exit_rate = effective_fee_rate(self._fee_rate(self.exit_fee_type), self.fee_gst_rate)
        stop_slippage_pct = (
            self.stop_slippage_pct
            if self.stop_slippage_pct is not None
            else self.slippage_pct
        )
        stop_slippage_rate = stop_slippage_pct / Decimal("100")
        return position.entry_price * (entry_rate + exit_rate + stop_slippage_rate)

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
        same_entry_candle = _position_opened_on_execution_candle(position, candle)
        if _bool_metadata(position.metadata, "bb_trail_force_close", False):
            return _exit_trigger(
                action=(
                    SignalAction.EXIT_LONG
                    if position.direction == SignalDirection.LONG
                    else SignalAction.EXIT_SHORT
                ),
                price=candle.open if not same_entry_candle else candle.close,
                reason="Bollinger Band hybrid trail force close ceiling reached.",
                trigger_type="stop_loss",
                trigger_level=candle.open if not same_entry_candle else candle.close,
                position=position,
            )
        if position.direction == SignalDirection.LONG:
            stop = position.stop_loss
            target = (
                position.take_profit
                if _bb_partial_close_enabled(position)
                else None if _trailing_priority_enabled(position) else position.take_profit
            )
            if not same_entry_candle and stop is not None and candle.open <= stop:
                return _exit_trigger(
                    action=SignalAction.EXIT_LONG,
                    price=candle.open,
                    reason=_gap_stop_reason(position),
                    trigger_type="stop_loss",
                    trigger_level=stop,
                    position=position,
                    gap_exit=True,
                )
            if not same_entry_candle and target is not None and candle.open >= target:
                if _bb_partial_close_enabled(position):
                    partial_pct = _decimal_metadata(
                        position.metadata,
                        "bb_trail_partial_close_pct",
                        Decimal("0.60"),
                    )
                    return _exit_trigger(
                        action=SignalAction.EXIT_LONG,
                        price=candle.open,
                        reason="bb_trail_partial_tp",
                        trigger_type="take_profit",
                        trigger_level=target,
                        position=position,
                        gap_exit=True,
                        extra_metadata={
                            "partial_close": True,
                            "partial_close_pct": partial_pct,
                            "partial_close_message": (
                                f"BB trail partial close: {partial_pct * Decimal('100')}% "
                                f"at TP {target}"
                            ),
                        },
                    )
                return _exit_trigger(
                    action=SignalAction.EXIT_LONG,
                    price=candle.open,
                    reason=_gap_take_profit_reason(position),
                    trigger_type="take_profit",
                    trigger_level=target,
                    position=position,
                    gap_exit=True,
                )
            stop_hit = _long_stop_hit(position, candle, stop)
            target_hit = _long_target_hit(position, candle, target)
            if stop_hit and target_hit:
                assert stop is not None
                return _exit_trigger(
                    action=SignalAction.EXIT_LONG,
                    price=stop,
                    reason="Ambiguous candle hit stop loss and take profit; conservative stop loss used.",
                    trigger_type="stop_loss",
                    trigger_level=stop,
                    position=position,
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
                    position=position,
                )
            if target_hit:
                assert target is not None
                if _bb_partial_close_enabled(position):
                    partial_pct = _decimal_metadata(
                        position.metadata,
                        "bb_trail_partial_close_pct",
                        Decimal("0.60"),
                    )
                    return _exit_trigger(
                        action=SignalAction.EXIT_LONG,
                        price=target,
                        reason="bb_trail_partial_tp",
                        trigger_type="take_profit",
                        trigger_level=target,
                        position=position,
                        extra_metadata={
                            "partial_close": True,
                            "partial_close_pct": partial_pct,
                            "partial_close_message": (
                                f"BB trail partial close: {partial_pct * Decimal('100')}% "
                                f"at TP {target}"
                            ),
                        },
                    )
                return _exit_trigger(
                    action=SignalAction.EXIT_LONG,
                    price=target,
                    reason=_take_profit_reason(position),
                    trigger_type="take_profit",
                    trigger_level=target,
                    position=position,
                )
            time_stop = _time_stop_trigger(position, candle)
            if time_stop is not None:
                return time_stop
            return None

        stop = position.stop_loss
        target = (
            position.take_profit
            if _bb_partial_close_enabled(position)
            else None if _trailing_priority_enabled(position) else position.take_profit
        )
        if not same_entry_candle and stop is not None and candle.open >= stop:
            return _exit_trigger(
                action=SignalAction.EXIT_SHORT,
                price=candle.open,
                reason=_gap_stop_reason(position),
                trigger_type="stop_loss",
                trigger_level=stop,
                position=position,
                gap_exit=True,
            )
        if not same_entry_candle and target is not None and candle.open <= target:
            if _bb_partial_close_enabled(position):
                partial_pct = _decimal_metadata(
                    position.metadata,
                    "bb_trail_partial_close_pct",
                    Decimal("0.60"),
                )
                return _exit_trigger(
                    action=SignalAction.EXIT_SHORT,
                    price=candle.open,
                    reason="bb_trail_partial_tp",
                    trigger_type="take_profit",
                    trigger_level=target,
                    position=position,
                    gap_exit=True,
                    extra_metadata={
                        "partial_close": True,
                        "partial_close_pct": partial_pct,
                        "partial_close_message": (
                            f"BB trail partial close: {partial_pct * Decimal('100')}% "
                            f"at TP {target}"
                        ),
                    },
                )
            return _exit_trigger(
                action=SignalAction.EXIT_SHORT,
                price=candle.open,
                reason=_gap_take_profit_reason(position),
                trigger_type="take_profit",
                trigger_level=target,
                position=position,
                gap_exit=True,
            )
        stop_hit = _short_stop_hit(position, candle, stop)
        target_hit = _short_target_hit(position, candle, target)
        if stop_hit and target_hit:
            assert stop is not None
            return _exit_trigger(
                action=SignalAction.EXIT_SHORT,
                price=stop,
                reason="Ambiguous candle hit stop loss and take profit; conservative stop loss used.",
                trigger_type="stop_loss",
                trigger_level=stop,
                position=position,
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
                position=position,
            )
        if target_hit:
            assert target is not None
            if _bb_partial_close_enabled(position):
                partial_pct = _decimal_metadata(
                    position.metadata,
                    "bb_trail_partial_close_pct",
                    Decimal("0.60"),
                )
                return _exit_trigger(
                    action=SignalAction.EXIT_SHORT,
                    price=target,
                    reason="bb_trail_partial_tp",
                    trigger_type="take_profit",
                    trigger_level=target,
                    position=position,
                    extra_metadata={
                        "partial_close": True,
                        "partial_close_pct": partial_pct,
                        "partial_close_message": (
                            f"BB trail partial close: {partial_pct * Decimal('100')}% "
                            f"at TP {target}"
                        ),
                    },
                )
            return _exit_trigger(
                action=SignalAction.EXIT_SHORT,
                price=target,
                reason=_take_profit_reason(position),
                trigger_type="take_profit",
                trigger_level=target,
                position=position,
            )
        time_stop = _time_stop_trigger(position, candle)
        if time_stop is not None:
            return time_stop
        return None

    def _update_trailing_stop(
        self,
        position: PaperPosition,
        candle: OHLCVCandle,
    ) -> None:
        trailing_stop_enabled = _bool_metadata(
            position.metadata,
            "trailing_stop_enabled",
            self.trailing_stop_enabled,
        )
        if not trailing_stop_enabled:
            return
        # If this position has ATR trailing enabled (via metadata or fallback), it takes 
        # precedence over the fixed percentage trailing stop.
        if (
            _bool_metadata(position.metadata, "atr_trailing_enabled", False)
            and _bool_metadata(position.metadata, "atr_dynamic_exits_enabled", False)
        ):
            return
        if position.stop_loss is None or candle.close <= 0:
            return

        reference_price = (
            candle.high
            if position.direction == SignalDirection.LONG
            else candle.low
        )
        candidate = self._trailing_stop_candidate(
            position,
            reference_price,
            activation_pct=_decimal_metadata(
                position.metadata,
                "trailing_stop_activation_pct",
                self.trailing_stop_activation_pct,
            ),
            distance_pct=_decimal_metadata(
                position.metadata,
                "trailing_stop_distance_pct",
                self.trailing_stop_distance_pct,
            ),
        )
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
            "take_profit_suppressed_by_trailing": _trailing_priority_enabled(position),
            "last_trailing_stop": candidate,
            "last_trailing_reference_price": reference_price,
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
        *,
        activation_pct: Decimal | None = None,
        distance_pct: Decimal | None = None,
    ) -> Decimal | None:
        activation = (activation_pct or self.trailing_stop_activation_pct) / Decimal("100")
        distance = (distance_pct or self.trailing_stop_distance_pct) / Decimal("100")

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


def _position_opened_on_execution_candle(
    position: PaperPosition,
    candle: OHLCVCandle,
) -> bool:
    entry_open_ms = _int_metadata(
        position.metadata,
        "entry_execution_candle_open_time_ms",
        -1,
    )
    if entry_open_ms < 0:
        return False
    if entry_open_ms != candle.open_time_ms:
        return False
    entry_interval = str(position.metadata.get("entry_execution_interval") or "")
    return not entry_interval or entry_interval == candle.interval


def _long_stop_hit(
    position: PaperPosition,
    candle: OHLCVCandle,
    stop: Decimal | None,
) -> bool:
    if stop is None:
        return False
    if not _position_opened_on_execution_candle(position, candle):
        return candle.low <= stop
    entry_low = _decimal_metadata(
        position.metadata,
        "entry_execution_candle_low",
        candle.low,
    )
    return candle.close <= stop or (candle.low < entry_low and candle.low <= stop)


def _long_target_hit(
    position: PaperPosition,
    candle: OHLCVCandle,
    target: Decimal | None,
) -> bool:
    if target is None:
        return False
    if not _position_opened_on_execution_candle(position, candle):
        return candle.high >= target
    entry_high = _decimal_metadata(
        position.metadata,
        "entry_execution_candle_high",
        candle.high,
    )
    return candle.close >= target or (candle.high > entry_high and candle.high >= target)


def _short_stop_hit(
    position: PaperPosition,
    candle: OHLCVCandle,
    stop: Decimal | None,
) -> bool:
    if stop is None:
        return False
    if not _position_opened_on_execution_candle(position, candle):
        return candle.high >= stop
    entry_high = _decimal_metadata(
        position.metadata,
        "entry_execution_candle_high",
        candle.high,
    )
    return candle.close >= stop or (candle.high > entry_high and candle.high >= stop)


def _short_target_hit(
    position: PaperPosition,
    candle: OHLCVCandle,
    target: Decimal | None,
) -> bool:
    if target is None:
        return False
    if not _position_opened_on_execution_candle(position, candle):
        return candle.low <= target
    entry_low = _decimal_metadata(
        position.metadata,
        "entry_execution_candle_low",
        candle.low,
    )
    return candle.close <= target or (candle.low < entry_low and candle.low <= target)


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


def _adaptive_stop_management_settings(
    *,
    position: PaperPosition,
    current_atr: Decimal,
    entry_atr: Decimal,
    r_unit: Decimal,
    trailing_distance: Decimal,
) -> dict[str, object]:
    profile = str(
        position.metadata.get("adaptive_stop_profile")
        or position.metadata.get("atr_profile")
        or position.metadata.get("trade_mode")
        or "balanced"
    ).strip().lower()

    if "momentum" in profile or "ignition" in profile:
        breakeven_noise_factor = Decimal("0.75")
        profit_buffer_factor = Decimal("0.45")
    elif "breakout" in profile or "runner" in profile:
        breakeven_noise_factor = Decimal("0.90")
        profit_buffer_factor = Decimal("0.60")
    elif "defensive" in profile or "mean" in profile:
        breakeven_noise_factor = Decimal("0.80")
        profit_buffer_factor = Decimal("0.50")
    elif "trend" in profile:
        breakeven_noise_factor = Decimal("1.10")
        profit_buffer_factor = Decimal("0.85")
    else:
        breakeven_noise_factor = Decimal("1.00")
        profit_buffer_factor = Decimal("0.70")

    live_atr_r = current_atr / r_unit
    entry_atr_r = entry_atr / r_unit if entry_atr > 0 else live_atr_r
    trailing_distance_r = trailing_distance / r_unit
    volatility_ratio = current_atr / entry_atr if entry_atr > 0 else Decimal("1")

    breakeven_activation_r = _clamp_decimal(
        live_atr_r * breakeven_noise_factor,
        Decimal("0.30"),
        Decimal("2.20"),
    )
    profit_lock_activation_r = max(
        breakeven_activation_r + (live_atr_r * profit_buffer_factor),
        trailing_distance_r + (live_atr_r * Decimal("0.25")),
    )
    profit_lock_activation_r = _clamp_decimal(
        profit_lock_activation_r,
        breakeven_activation_r,
        Decimal("4.00"),
    )
    profit_lock_r = max(
        Decimal("0"),
        profit_lock_activation_r - trailing_distance_r,
    )
    profit_lock_r = min(
        profit_lock_r,
        profit_lock_activation_r * Decimal("0.70"),
    )

    return {
        "adaptive_stop_profile": profile,
        "adaptive_breakeven_activation_r": breakeven_activation_r,
        "adaptive_profit_lock_activation_r": profit_lock_activation_r,
        "adaptive_profit_lock_r": profit_lock_r,
        "adaptive_live_atr_r": live_atr_r,
        "adaptive_entry_atr_r": entry_atr_r,
        "adaptive_trailing_distance_r": trailing_distance_r,
        "adaptive_volatility_ratio": volatility_ratio,
        "breakeven_activation_r": breakeven_activation_r,
        "profit_lock_activation_r": profit_lock_activation_r,
        "profit_lock_r": profit_lock_r,
    }


def _clamp_decimal(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(max(value, low), high)


def _can_scale_in(signal: StrategySignal, position: PaperPosition) -> bool:
    return (
        bool(signal.metadata.get("allow_scale_in"))
        and signal.direction == position.direction
        and signal.strategy_name == position.strategy_name
    )


def _time_stop_trigger(
    position: PaperPosition,
    candle: OHLCVCandle,
) -> _ExitTrigger | None:
    time_stop_ms = _int_metadata(position.metadata, "time_stop_ms", 0)
    if time_stop_ms <= 0:
        return None
    if candle.close_time_ms < position.opened_at_ms + time_stop_ms:
        return None
    exit_only_if_stagnant = _bool_metadata(
        position.metadata,
        "time_stop_exit_only_if_stagnant",
        True,
    )
    if exit_only_if_stagnant:
        min_r = _decimal_metadata(
            position.metadata,
            "time_stop_min_r",
            Decimal("0"),
        )
        current_r = _position_current_r(position, candle.close)
        if current_r is not None:
            if current_r > min_r:
                return None
        elif _position_profit(position, candle.close) > 0:
            return None

    if _bool_metadata(position.metadata, "time_stop_extend_if_momentum_strong", True):
        # momentum is strong if it's a profitable impulse candle in our direction
        is_strong = False
        if position.direction == SignalDirection.LONG:
            if candle.close > candle.open and candle.close > position.entry_price:
                is_strong = True
        else:
            if candle.close < candle.open and candle.close < position.entry_price:
                is_strong = True
        if is_strong:
            return None

    return _exit_trigger(
        action=(
            SignalAction.EXIT_LONG
            if position.direction == SignalDirection.LONG
            else SignalAction.EXIT_SHORT
        ),
        price=candle.close,
        reason="Stagnation stop triggered: breakout did not progress after the time window.",
        trigger_type="stagnation_time_stop",
        trigger_level=candle.close,
        position=position,
    )


def _bb_partial_close_enabled(position: PaperPosition) -> bool:
    if not _bool_metadata(position.metadata, "bb_trail_enabled", False):
        return False
    if not _bool_metadata(position.metadata, "bb_trail_active", False):
        return False
    if _bool_metadata(position.metadata, "bb_partial_close_executed", False):
        return False
    if not _bool_metadata(position.metadata, "tp_is_partial_close", False):
        return False
    partial_pct = _decimal_metadata(
        position.metadata,
        "bb_trail_partial_close_pct",
        _decimal_metadata(position.metadata, "tp_partial_close_pct", Decimal("0")),
    )
    return Decimal("0") < partial_pct < Decimal("1")


def _trailing_priority_enabled(position: PaperPosition) -> bool:
    """Return True when a position should be managed by a trailing/profit stop.

    In this mode take-profit is treated as a diagnostic/initial target, not a
    full-close trigger. The trade exits when the stop ratchets up/down and the
    market reverses into it.
    """

    if str(position.metadata.get("take_profit_priority") or "").lower() == "take_profit":
        return False
    if _bool_metadata(position.metadata, "profit_lock_enabled", False):
        return True
    if _bool_metadata(position.metadata, "bb_trail_active", False):
        return True
    if (
        _dynamic_atr_exits_enabled(position)
        and _bool_metadata(position.metadata, "atr_trailing_enabled", False)
    ):
        return True
    if _bool_metadata(position.metadata, "trailing_stop_enabled", False):
        return True
    return False


def _dynamic_atr_exits_enabled(position: PaperPosition) -> bool:
    if "atr_dynamic_exits_enabled" in position.metadata:
        return _bool_metadata(position.metadata, "atr_dynamic_exits_enabled", False)
    return any(
        _bool_metadata(position.metadata, key, False)
        for key in (
            "atr_stop_enabled",
            "atr_take_profit_enabled",
            "atr_trailing_enabled",
            "breakeven_enabled",
            "profit_lock_enabled",
            "bb_trail_enabled",
        )
    )


def _position_profit(position: PaperPosition, price: Decimal) -> Decimal:
    if position.direction == SignalDirection.LONG:
        return price - position.entry_price
    return position.entry_price - price


def _position_current_r(position: PaperPosition, price: Decimal) -> Decimal | None:
    initial_stop = _decimal_metadata(position.metadata, "initial_stop_loss", None)
    if initial_stop is None or initial_stop <= 0:
        return None
    risk_unit = abs(position.entry_price - initial_stop)
    if risk_unit <= 0:
        return None
    return _position_profit(position, price) / risk_unit


def _exit_trigger(
    *,
    action: SignalAction,
    price: Decimal,
    reason: str,
    trigger_type: str,
    trigger_level: Decimal,
    position: PaperPosition, # Task: Pass position for context
    gap_exit: bool = False,
    ambiguous_candle: bool = False,
    extra_metadata: dict[str, object] | None = None,
) -> _ExitTrigger:
    metadata = {
        "exit_trigger_type": trigger_type,
        "exit_trigger_level": trigger_level,
        "trigger_price_source": "candle_open" if gap_exit else "trigger_level",
        "gap_exit": gap_exit,
        "ambiguous_candle": ambiguous_candle,
        "initial_stop_loss": position.stop_loss,
        "take_profit": position.take_profit,
        "atr_stop_loss": position.metadata.get("atr_stop_loss"),
        "atr_take_profit": position.metadata.get("atr_take_profit"),
        "stop_type": position.metadata.get("stop_type"),
        "atr_dynamic_exit_active": position.metadata.get("atr_dynamic_exit_active"),
        "atr_stop_enabled": position.metadata.get("atr_stop_enabled"),
        "trailing_stop_active": position.metadata.get("trailing_stop_active"),
        "take_profit_suppressed_by_trailing": _trailing_priority_enabled(position),
        "take_profit_suppressed_by_bb_trail": position.metadata.get("take_profit_suppressed_by_bb_trail"),
        "bb_trail_enabled": position.metadata.get("bb_trail_enabled"),
        "bb_trail_active": position.metadata.get("bb_trail_active"),
        "bb_trail_stop": position.metadata.get("bb_trail_stop"),
        "bb_trail_stage": position.metadata.get("bb_trail_stage"),
        "bb_mid": position.metadata.get("bb_mid"),
        "bb_upper": position.metadata.get("bb_upper"),
        "bb_lower": position.metadata.get("bb_lower"),
        "bb_buffer_atr": position.metadata.get("bb_buffer_atr"),
        "bb_entry_band_position": position.metadata.get("bb_entry_band_position"),
        "bb_entry_gate_passed": position.metadata.get("bb_entry_gate_passed"),
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return _ExitTrigger(
        action=action,
        price=price,
        reason=reason,
        metadata=metadata,
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
        "initial_stop_loss",
        "take_profit",
        "atr_stop_loss",
        "atr_take_profit",
        "stop_type",
        "atr_dynamic_exit_active",
        "atr_stop_enabled",
        "trailing_stop_active",
        "take_profit_suppressed_by_trailing",
        "take_profit_suppressed_by_bb_trail",
        "bb_trail_enabled",
        "bb_trail_active",
        "bb_trail_stop",
        "bb_trail_stage",
        "bb_mid",
        "bb_upper",
        "bb_lower",
        "bb_buffer_atr",
        "bb_entry_band_position",
        "bb_entry_gate_passed",
        "partial_close",
        "partial_close_pct",
        "partial_close_message",
    }
    metadata = {key: signal.metadata[key] for key in keys if key in signal.metadata}
    metadata["reason"] = signal.reason
    return metadata


def _normalize_fee_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"maker", "taker"}:
        raise ValueError("fee type must be maker or taker.")
    return normalized


def _sequence_number(value: object, prefix: str) -> int:
    text = str(value or "")
    if not text.startswith(prefix):
        return 0
    try:
        return int(text[len(prefix) :])
    except ValueError:
        return 0


def _saved_state_currency_compatible(
    saved: Mapping[str, Any],
    *,
    quote_to_margin_rate: Decimal,
    unit_contract_value: Decimal,
) -> bool:
    """Reject old paper state that was recorded before INR conversion metadata.

    Restoring those fills into an INR-M session makes dashboard equity, PnL, fees
    and margin look valid while they are actually quote-currency numbers.
    """

    def decimal_or_none(value: object) -> Decimal | None:
        if value is None or value == "":
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    positions = saved.get("positions") or {}
    if isinstance(positions, Mapping):
        for raw_position in positions.values():
            if not isinstance(raw_position, Mapping):
                continue
            rate = decimal_or_none(raw_position.get("quote_to_margin_rate"))
            unit = decimal_or_none(raw_position.get("unit_contract_value"))
            metadata = raw_position.get("metadata")
            if isinstance(metadata, Mapping):
                rate = rate or decimal_or_none(metadata.get("quote_to_margin_rate"))
                unit = unit or decimal_or_none(metadata.get("unit_contract_value"))
            if rate is None:
                rate = Decimal("1")
            if unit is None:
                unit = Decimal("1")
            if rate != quote_to_margin_rate or unit != unit_contract_value:
                return False

    fills = saved.get("fills") or []
    if isinstance(fills, list):
        for raw_fill in fills:
            if not isinstance(raw_fill, Mapping):
                continue
            metadata = raw_fill.get("metadata")
            if not isinstance(metadata, Mapping):
                metadata = {}
            rate = decimal_or_none(metadata.get("quote_to_margin_rate"))
            unit = decimal_or_none(metadata.get("unit_contract_value"))
            if rate is None:
                rate = Decimal("1")
            if unit is None:
                unit = Decimal("1")
            if rate != quote_to_margin_rate or unit != unit_contract_value:
                return False

    return True


def _quantity_unit(pair: str) -> str:
    symbol = pair
    if symbol.startswith("B-"):
        symbol = symbol[2:]
    return symbol.split("_", 1)[0] or "contracts"


def _slippage_pct_from_fill(
    *,
    base_price: Decimal,
    fill_price: Decimal,
    side: PaperOrderSide,
) -> Decimal:
    if base_price <= 0:
        return Decimal("0")
    if side == PaperOrderSide.BUY:
        return (fill_price - base_price) / base_price * Decimal("100")
    return (base_price - fill_price) / base_price * Decimal("100")


def _spread_pct(
    best_bid: Decimal | None,
    best_ask: Decimal | None,
) -> Decimal | None:
    if best_bid is None or best_ask is None:
        return None
    midpoint = (best_bid + best_ask) / Decimal("2")
    if midpoint <= 0:
        return None
    return (best_ask - best_bid) / midpoint * Decimal("100")


def _int_metadata(metadata: Mapping[str, object], key: str, default: int) -> int:
    value = metadata.get(key)
    if value is None:
        return default
    try:
        return int(Decimal(str(value)))
    except Exception:
        return default


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
    # Exit priority (highest to lowest):
    # 1. hard stop loss (initial protective stop)
    # 2. profit lock stop
    # 3. bb_trail - Bollinger Band hybrid trail (when bb_trail_active=True)
    # 4. ATR dynamic trail
    # 5. normal trailing stop (pct-based)
    # 6. strategy invalidation
    # 7. take profit (or partial close if bb_trail_partial_close_at_tp=True)
    stop_type = str(position.metadata.get("stop_type", "")).strip().lower()
    if stop_type == "bb_trail":
        return "Bollinger Band hybrid trail stop triggered."
    if position.metadata.get("atr_dynamic_exit_active") and position.metadata.get("atr_stop_enabled"):
        if stop_type == "breakeven":
            return "Breakeven stop triggered."
        if stop_type == "profit_lock":
            return "Profit lock stop triggered."
        return "Dynamic ATR stop triggered."
    if position.metadata.get("trailing_stop_active"):
        return "Trailing stop triggered."
    return "Stop loss triggered."


def _take_profit_reason(position: PaperPosition) -> str:
    if position.metadata.get("atr_dynamic_exit_active") and position.metadata.get("atr_take_profit_enabled"):
        return "Dynamic ATR take profit triggered."
    return "Take profit triggered."


def _gap_stop_reason(position: PaperPosition) -> str:
    stop_type = str(position.metadata.get("stop_type", "")).strip().lower()
    if stop_type == "bb_trail":
        return "Bollinger Band hybrid trail stop gapped through; filled at candle open."
    if position.metadata.get("atr_dynamic_exit_active") and position.metadata.get("atr_stop_enabled"):
        if stop_type == "breakeven":
            return "Breakeven stop gapped through; filled at candle open."
        if stop_type == "profit_lock":
            return "Profit lock stop gapped through; filled at candle open."
        return "Dynamic ATR stop gapped through; filled at candle open."
    if position.metadata.get("trailing_stop_active") and not position.metadata.get("fixed_trailing_stop_suppressed"):
        return "Trailing stop gapped through; filled at candle open."
    return "Stop loss gapped through; filled at candle open."


def _gap_take_profit_reason(position: PaperPosition) -> str:
    if position.metadata.get("atr_dynamic_exit_active") and position.metadata.get("atr_take_profit_enabled"):
        return "Dynamic ATR take profit gapped through; filled at candle open."
    return "Take profit gapped through; filled at candle open."
