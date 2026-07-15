from __future__ import annotations

import logging
import threading
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.config import RiskSettings, Settings
from app.data.candle_builder import CandleSeries, OHLCVCandle, interval_to_ms
from app.data.gap_guard import CandleGapGuard
from app.data.indicators import BollingerBandPoint, bollinger_bands, latest_indicator_snapshot
from app.data.pipeline import MarketDataPipeline
from app.execution.live import LiveExecutionEngine, LiveExecutionReport
from app.exchange.coindcx_rest import CoinDCXFuturesClient
from app.exchange.coindcx_ws import CoinDCXFuturesWebSocketClient, MarketSubscription
from app.exchange.coindcx_channels import futures_candle_channel, futures_orderbook_channel
from app.persistence.paper_state import PaperStateStore, PaperSessionStore
from app.broker.paper import PaperBroker
from app.broker.models import PaperFill, PaperOrderSide, PaperPosition, PaperExecutionReport
from app.risk.manager import RiskManager
from app.risk.models import OpenPosition, RiskDecision
from app.risk.limits import is_entry_signal, is_exit_signal
from app.risk.pair_profiles import apply_pair_profile_to_config, pair_profile_for
from app.risk.pair_performance import pair_recent_risk_profile
from app.risk.profit_protection import post_profit_pullback_seen
from app.strategies.base import StrategyEngine, StrategyContext, SignalAction, SignalDirection, StrategySignal
from app.strategies.defaults import STRATEGY_CHOICES, strategy_engine_for_name
from app.strategies.entry_quality import (
    A_SETUP_AGREEMENT_THRESHOLD,
    B_SETUP_AGREEMENT_THRESHOLD,
    DEFAULT_DIRECTIONAL_ENTRY_THRESHOLD,
)
from app.utils.json import to_jsonable
from app.live.state import LivePaperState, _update_live_state, get_live_state, reset_live_state
from app.live.summary_logger import PaperTradingSummaryLogger


def _decimal_metadata(value: Any, default: Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _fmt_diag_decimal(value: Any, *, places: int) -> str:
    try:
        decimal_value = Decimal(str(value))
    except Exception:
        return "-"
    quant = Decimal("1").scaleb(-places)
    return str(decimal_value.quantize(quant))


def _bool_metadata(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _closed_position_direction(fill: PaperFill) -> SignalDirection | None:
    if fill.side == PaperOrderSide.SELL:
        return SignalDirection.LONG
    if fill.side == PaperOrderSide.BUY:
        return SignalDirection.SHORT
    return None


def _is_position_close_fill(fill: PaperFill) -> bool:
    metadata = fill.metadata if isinstance(fill.metadata, dict) else {}
    return (
        fill.realized_pnl != 0
        or bool(metadata.get("exit_trigger_type"))
        or bool(metadata.get("reason"))
    )


def _paper_exit_reason(fill: PaperFill) -> str:
    metadata = fill.metadata if isinstance(fill.metadata, dict) else {}
    trigger_type = str(metadata.get("exit_trigger_type") or "").strip()
    stop_type = str(metadata.get("stop_type") or "").strip().lower()
    detail = str(metadata.get("reason") or "").strip()
    detail_l = detail.lower()

    if trigger_type == "stop_loss":
        if stop_type == "bb_trail" or "bollinger band hybrid trail" in detail_l:
            return "bb_trail_stop"
        if stop_type == "breakeven" or "breakeven" in detail_l:
            return "breakeven_stop"
        if stop_type == "profit_lock" or "profit lock" in detail_l:
            return "profit_lock_stop"
        if stop_type == "profit_giveback" or "profit giveback" in detail_l:
            return "profit_giveback_stop"
        if (
            stop_type == "atr"
            or (
                _bool_metadata(metadata.get("atr_dynamic_exit_active"), False)
                and _bool_metadata(metadata.get("atr_stop_enabled"), False)
            )
            or "dynamic atr" in detail_l
        ):
            return "dynamic_atr_stop"
        if _bool_metadata(metadata.get("trailing_stop_active"), False) or "trailing" in detail_l:
            return "trailing_stop"
        return "stop_loss"
    if trigger_type == "take_profit":
        if _bool_metadata(metadata.get("partial_close"), False) or detail == "bb_trail_partial_tp":
            return "bb_trail_partial_tp"
        if "dynamic atr" in detail_l:
            return "dynamic_atr_take_profit"
        return "take_profit"
    if trigger_type == "strategy_momentum_exit":
        if "rsi/macd" in detail_l:
            return "rsi_macd_exit"
        return "strategy_momentum_exit"
    return trigger_type or detail or "signal"


def _paper_stop_style_exit(exit_reason: str) -> bool:
    normalized = exit_reason.lower().replace(" ", "_")
    return (
        "stop" in normalized
        or normalized in {"dynamic_atr_stop", "profit_lock_stop", "breakeven_stop"}
    )


def _paper_take_profit_style_exit(exit_reason: str) -> bool:
    normalized = exit_reason.lower().replace(" ", "_")
    return "take_profit" in normalized


def _position_stop_management_active(metadata: dict[str, Any]) -> bool:
    return (
        bool(metadata.get("stop_type"))
        or _bool_metadata(metadata.get("bb_trail_active"), False)
        or _bool_metadata(metadata.get("trailing_stop_active"), False)
        or _bool_metadata(metadata.get("atr_dynamic_exit_active"), False)
    )


def _position_take_profit_suppressed(metadata: dict[str, Any]) -> bool:
    if _bool_metadata(metadata.get("take_profit_suppressed_by_bb_trail"), False):
        return True
    if _bool_metadata(metadata.get("take_profit_suppressed_by_trailing"), False):
        return True
    if _bool_metadata(metadata.get("profit_lock_enabled"), False):
        return True
    if (
        _bool_metadata(metadata.get("atr_dynamic_exits_enabled"), False)
        and _bool_metadata(metadata.get("atr_trailing_enabled"), False)
    ):
        return True
    return _bool_metadata(metadata.get("trailing_stop_enabled"), False)


def _position_stop_type(metadata: dict[str, Any]) -> str:
    raw = str(metadata.get("stop_type") or "").strip().lower()
    if raw in {"bb_trail", "bb trail"}:
        return "BB Trail"
    if raw in {"profit_lock", "profit lock"}:
        return "profit lock"
    if raw in {"breakeven", "break even"}:
        return "breakeven"
    if raw == "atr":
        return "ATR"
    if _bool_metadata(metadata.get("trailing_stop_active"), False):
        return "trailing"
    if _bool_metadata(metadata.get("atr_dynamic_exit_active"), False):
        return "ATR"
    if _bool_metadata(metadata.get("profit_lock_enabled"), False):
        return "profit lock pending"
    if _bool_metadata(metadata.get("atr_trailing_enabled"), False):
        return "ATR pending"
    if _bool_metadata(metadata.get("trailing_stop_enabled"), False):
        return "trailing pending"
    return ""


def _quantity_unit(pair: str) -> str:
    symbol = pair
    if symbol.startswith("B-"):
        symbol = symbol[2:]
    return symbol.split("_", 1)[0] or "contracts"


def _paper_reentry_override(signal: Any, *, strict: bool) -> bool:
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    final_score = abs(_decimal_metadata(metadata.get("final_score"), Decimal("0")))
    agreement = _decimal_metadata(
        metadata.get("agreement_ratio")
        or metadata.get("long_agreement_ratio")
        or metadata.get("short_agreement_ratio"),
        Decimal("0"),
    )
    visual_score = _decimal_metadata(metadata.get("visual_score"), Decimal("0"))
    visual = metadata.get("visual")
    visual_blocked = isinstance(visual, dict) and bool(visual.get("blocked"))
    if visual_blocked:
        return False

    if strict:
        return (
            final_score >= Decimal("0.70")
            and agreement >= Decimal("0.75")
            and visual_score >= Decimal("0.15")
        )
    if metadata.get("momentum_ignition"):
        return True
    if metadata.get("entry_type") == "intrabar_reversal_breakout":
        return True
    return (
        final_score >= Decimal("0.62")
        and agreement >= Decimal("0.68")
        and visual_score >= Decimal("0.05")
    )


_active_loop: PaperTradingLoop | None = None


def get_active_loop() -> PaperTradingLoop | None:
    return _active_loop


def set_active_loop(loop: PaperTradingLoop | None) -> None:
    global _active_loop
    _active_loop = loop


class PaperTradingLoop:
    def __init__(
        self, 
        settings: Settings, 
        strategy_name: str = "adaptive_hybrid",
        state_store: PaperStateStore | None = None,
        session_store: PaperSessionStore | None = None,
        pair_overrides: dict[str, dict[str, Any]] | None = None,
        execution_mode: str = "paper",
    ) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        normalized_execution_mode = str(execution_mode or "paper").strip().lower()
        if normalized_execution_mode not in {"paper", "live_dry_run", "live_pilot"}:
            raise ValueError(f"Unsupported execution mode: {execution_mode}")
        self.execution_mode = normalized_execution_mode
        self.live_dry_run = normalized_execution_mode != "live_pilot"
        
        self.strategy_engine = strategy_engine_for_name(strategy_name)
        self._strategy_engines_by_name: dict[str, StrategyEngine] = {
            strategy_name: self.strategy_engine
        }
        self.risk_manager = RiskManager(settings.risk)
        self.state_store = state_store or PaperStateStore()
        self.session_store = session_store or PaperSessionStore() # Task 1
        
        self.broker = PaperBroker(
            starting_equity=settings.paper_starting_equity,
            maker_fee_rate=settings.risk.maker_fee_rate,
            taker_fee_rate=settings.risk.taker_fee_rate,
            fee_gst_rate=settings.risk.fee_gst_rate,
            slippage_pct=settings.risk.slippage_pct,
            stop_slippage_pct=settings.risk.stop_slippage_pct,
            trailing_stop_enabled=settings.risk.trailing_stop_enabled,
            trailing_stop_activation_pct=settings.risk.trailing_stop_activation_pct,
            trailing_stop_distance_pct=settings.risk.trailing_stop_distance_pct,
            quote_to_margin_rate=settings.quote_to_margin_rate,
            account_currency=settings.futures_margin_currency,
            price_quote_currency=settings.price_quote_currency,
            state_store=self.state_store,
            logger=logging.getLogger("app.broker.paper")
        )
        self.broker.profit_lock_enabled = settings.risk.profit_lock_enabled
        
        self.summary_logger = PaperTradingSummaryLogger()
        self.client = CoinDCXFuturesClient(settings)
        self.live_execution_engine: LiveExecutionEngine | None = (
            LiveExecutionEngine(self.client, settings, dry_run=self.live_dry_run)
            if self._live_execution_enabled
            else None
        )
        self._live_execution_reports: list[dict[str, Any]] = []
        self._live_positions: dict[str, dict[str, Any]] = {}
        self._live_orders: list[dict[str, Any]] = []
        self._live_sync_status: dict[str, Any] = {}
        
        # Multi-pair infrastructure
        self.series: dict[str, CandleSeries] = {}
        self.execution_series: dict[str, CandleSeries] = {}
        self.gap_guards: dict[str, CandleGapGuard] = {}
        
        self.candle_count = 0
        self.equity_history: list[dict[str, Any]] = [] # Task 2
        self.candle_history: dict[str, list[dict[str, Any]]] = {}
        self._position_entry_candle: dict[str, int] = {}
        self._position_entry_prices: dict[str, Decimal] = {}
        
        self._watchlist: list[str] = []
        self._watchlist_lock = threading.RLock()
        self._subscriptions: list[MarketSubscription] = []
        self._current_interval = "unknown"
        self._ws_client = None
        self._stop_requested = False
        self._closed_count: int = 0
        self._entry_type_counts: dict[str, int] = {}
        from collections import deque
        self._recent_diagnostics: deque[dict[str, Any]] = deque(maxlen=20)
        self._entries_this_parent_candle: dict[str, int] = {}
        self._current_parent_open_ms: dict[str, int] = {}
        self._last_closed_strategy_eval_ms: dict[str, int] = {}
        self._session_started_at = datetime.now(timezone.utc).isoformat()
        self._pending_stream_candles: dict[tuple[str, str], OHLCVCandle] = {}
        self._pair_cooldown_until_ms: dict[str, int] = {}
        self._same_direction_cooldown_until_ms: dict[tuple[str, str], int] = {}
        self._same_direction_reversal_cooldown_until_ms: dict[tuple[str, str], int] = {}
        self._post_profit_reentry_state: dict[str, dict[str, Any]] = {}
        self._pair_recent_net_pnls: dict[str, list[Decimal]] = {}
        self._pair_overrides: dict[str, dict[str, Any]] = {}
        self._global_loss_cooldown_until_ms: int = 0
        self._consecutive_losing_trades: int = 0
        explicit_pair_overrides = pair_overrides is not None
        if pair_overrides:
            self.update_pair_overrides(pair_overrides, publish=False)
        
        self._init_audit_log()

        # Task 4: Restore state
        if self.broker.restored_state_ignored:
            self.state_store.clear()
            self.session_store.clear()
        else:
            self._load_session()
            if explicit_pair_overrides:
                self.update_pair_overrides(pair_overrides or {}, publish=False)

    def _init_audit_log(self) -> None:
        import os
        import csv
        from pathlib import Path
        path = Path("data/paper_intrabar_audit.csv")
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "pair", "strategy_interval", "execution_interval",
                    "open", "high", "low", "close", "volume",
                    "previous_parent_high", "previous_parent_low",
                    "candidate_side", "candidate_entry_type", "decision", "rejection_reason",
                    "volume_ratio", "body_ratio", "close_position_ratio", "wick_ratio",
                    "fast_ema", "atr", "extension_atr", "breakout_age_candles",
                    "consecutive_impulse_candles", "volume_fade_state",
                    "false_breakout_filter_result", "late_chase_blocked",
                    "higher_context_state", "risk_approved", "risk_rejection_reason",
                    "open_position_count", "same_pair_position_open",
                    "total_open_positions"
                ])

    def _log_audit(self, row: list[Any]) -> None:
        import csv
        import os
        from pathlib import Path
        from datetime import datetime
        
        path = Path("data/paper_intrabar_audit.csv")
        
        # Check rotation
        max_size_bytes = self.settings.audit_max_file_mb * 1024 * 1024
        if path.exists() and path.stat().st_size >= max_size_bytes:
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            rotated_path = path.with_suffix(f".{timestamp_str}.csv")
            try:
                 path.rename(rotated_path)
            except OSError:
                 pass
            
            # Keep only 3 latest rotated files
            audit_dir = path.parent
            rotated_files = sorted(audit_dir.glob("paper_intrabar_audit.*.csv"), key=lambda p: p.stat().st_mtime)
            while len(rotated_files) > 3:
                oldest = rotated_files.pop(0)
                try:
                    oldest.unlink()
                except OSError:
                    pass
                    
            self._init_audit_log()
            
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)

    @property
    def _live_execution_enabled(self) -> bool:
        return self.execution_mode in {"live_dry_run", "live_pilot"}

    def _record_live_execution_report(self, report: LiveExecutionReport) -> None:
        data = report.to_dict()
        self._live_execution_reports.append(data)
        if len(self._live_execution_reports) > 50:
            del self._live_execution_reports[:-50]

        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        sync = metadata.get("live_sync") if isinstance(metadata.get("live_sync"), dict) else {}
        signal = data.get("signal") if isinstance(data.get("signal"), dict) else {}
        pair = str(signal.get("pair") or "")

        if sync:
            self._live_sync_status = {
                "accepted": report.accepted,
                "dry_run": report.dry_run,
                "reason": report.reason,
                "sync": sync,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            position = sync.get("position")
            if isinstance(position, dict) and position.get("pair"):
                self._live_positions[str(position["pair"])] = position
            orders = sync.get("orders")
            if isinstance(orders, list):
                self._live_orders = [item for item in orders if isinstance(item, dict)][-50:]
        else:
            self._live_sync_status = {
                "accepted": report.accepted,
                "dry_run": report.dry_run,
                "reason": report.reason,
                "pair": pair,
                "order_request": data.get("order_request"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    def _live_position_id_from_report(self, report: LiveExecutionReport) -> str:
        metadata = report.metadata if isinstance(report.metadata, dict) else {}
        sync = metadata.get("live_sync") if isinstance(metadata.get("live_sync"), dict) else {}
        position = sync.get("position") if isinstance(sync.get("position"), dict) else {}
        return str(position.get("position_id") or "").strip()

    def _with_live_execution_metadata(
        self,
        decision: RiskDecision,
        report: LiveExecutionReport,
    ) -> RiskDecision:
        live_report = report.to_dict()
        live_metadata: dict[str, Any] = {
            "execution_mode": self.execution_mode,
            "live_dry_run": report.dry_run,
            "live_pilot": True,
            "live_execution_report": live_report,
        }
        position_id = self._live_position_id_from_report(report)
        if position_id:
            live_metadata["live_position_id"] = position_id
        if report.order_request:
            live_metadata["live_order_request"] = report.order_request

        signal = replace(
            decision.signal,
            metadata={**decision.signal.metadata, **live_metadata},
        )
        return replace(
            decision,
            signal=signal,
            metadata={**decision.metadata, **live_metadata},
        )

    def _execute_live_decision(self, decision: RiskDecision) -> LiveExecutionReport:
        if self.live_execution_engine is None:
            return LiveExecutionReport(
                accepted=False,
                dry_run=True,
                reason="Live execution engine is not configured.",
                risk_decision=decision,
                signal=decision.signal,
                metadata={"live_pilot": True},
            )
        report = self.live_execution_engine.process_decision(decision)
        self._record_live_execution_report(report)
        if not report.accepted:
            _update_live_state(
                error=report.reason,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
            self.logger.error("[live_execution] %s", report.reason)
            if not report.dry_run:
                self._stop_requested = True
        return report

    def _mirror_live_shadow_exit_reports(
        self,
        reports: list[PaperExecutionReport],
        candle: OHLCVCandle,
    ) -> None:
        if not self._live_execution_enabled:
            return
        for report in reports:
            if not report.accepted or report.fill is None or report.signal is None:
                continue
            if not _is_position_close_fill(report.fill):
                continue
            signal = report.signal
            metadata = dict(signal.metadata)
            position_metadata = report.position.metadata if report.position is not None else {}
            position_id = str(position_metadata.get("live_position_id") or "").strip()
            if position_id:
                metadata["live_position_id"] = position_id
            exit_signal = replace(
                signal,
                timestamp_ms=candle.close_time_ms,
                metadata=metadata,
            )
            decision = RiskDecision(
                approved=True,
                reason=report.reason or "paper_shadow_exit",
                signal=exit_signal,
                metadata={"paper_shadow_exit": True},
            )
            live_report = self._execute_live_decision(decision)
            if not live_report.accepted:
                self.logger.error(
                    "[live_execution] Shadow exit for %s was not confirmed: %s",
                    candle.pair,
                    live_report.reason,
                )

    def _process_candle_for_execution_mode(
        self,
        candle: OHLCVCandle,
    ) -> list[PaperExecutionReport]:
        if not self._live_execution_enabled:
            return self.broker.process_candle(candle)
        return self._process_live_shadow_candle(candle)

    def _process_live_shadow_candle(
        self,
        candle: OHLCVCandle,
    ) -> list[PaperExecutionReport]:
        day = datetime.fromtimestamp(
            candle.close_time_ms / 1000,
            tz=timezone.utc,
        ).date().isoformat()
        if self.broker.last_pnl_reset_day != day:
            self.broker.daily_loss_from_tradable_base = Decimal("0")
            self.broker.daily_tradable_base_start = self.broker.tradable_base
            self.broker.protected_profit_override_enabled = False
            self.broker.last_pnl_reset_day = day
            self.broker._save_state()

        position = self.broker.positions.get(candle.pair)
        if position is None:
            return []

        trigger = self.broker._trigger_for_position(position, candle)
        if trigger is None:
            self.broker._update_trailing_stop(position, candle)
            self.broker._save_state()
            return []

        if trigger.metadata.get("partial_close"):
            message = (
                "Live partial close is not implemented yet; keeping shadow "
                f"position open for {position.pair}."
            )
            self.logger.error("[live_execution] %s", message)
            _update_live_state(
                error=message,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
            return []

        metadata = {
            "paper_trigger": True,
            **trigger.metadata,
            "live_position_id": str(position.metadata.get("live_position_id") or ""),
        }
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
            metadata=metadata,
        )
        live_decision = RiskDecision(
            approved=True,
            reason=trigger.reason,
            signal=signal,
            metadata={"paper_shadow_exit": True},
        )
        live_report = self._execute_live_decision(live_decision)
        if not live_report.accepted:
            return []

        shadow_decision = self._with_live_execution_metadata(live_decision, live_report)
        report = self.broker._close_from_signal(
            shadow_decision.signal,
            market_price=trigger.price,
            timestamp_ms=candle.close_time_ms,
            risk_decision=shadow_decision,
            reason=trigger.reason,
        )
        self.broker._save_state()
        return [report]

    def _write_audit_row(
        self, 
        candle: OHLCVCandle, 
        signal: StrategySignal
    ) -> None:
        m = signal.metadata
        decision_obj = m.get("risk_decision")
        
        # Determine Decision string: entered / rejected / no_candidate
        decision_str = "no_candidate"
        if signal.action != SignalAction.HOLD:
            if decision_obj and decision_obj.approved:
                decision_str = "entered"
            else:
                decision_str = "rejected"
        elif m.get("breakout_rejection"):
            decision_str = "rejected"

        rejection_reason = m.get("breakout_rejection") or ""
        if decision_obj and not decision_obj.approved:
            rejection_reason = f"risk_rejected: {decision_obj.reason}"
        elif m.get("safety_rejection"):
            rejection_reason = f"safety_rejected: {m['safety_rejection']}"
        elif signal.action == SignalAction.HOLD:
             rejection_reason = signal.reason

        # Extract features
        visual_metadata = m.get("visual") if isinstance(m.get("visual"), dict) else {}
        vol_ratio = (
            m.get("execution_volume_ratio")
            or m.get("volume_ratio")
            or visual_metadata.get("volume_ratio")
            or Decimal("0")
        )
        body_ratio = m.get("execution_body_ratio") or m.get("body_ratio") or Decimal("0")
        cp_ratio = m.get("execution_close_position_ratio") or m.get("close_position_ratio") or Decimal("0")
        
        candle_range = candle.high - candle.low
        wick_ratio = Decimal("0")
        if candle_range > 0:
            if signal.direction == SignalDirection.SHORT:
                wick_ratio = (candle.close - candle.low) / candle_range
            else:
                wick_ratio = (candle.high - candle.close) / candle_range
        try:
            bb_position = Decimal(str(m.get("bb_entry_band_position")))
            bb_position_text = f"{bb_position:.2f}"
        except Exception:
            bb_position_text = "-"
        bb_gate = "off"
        if _bool_metadata(m.get("bb_entry_gate_enabled"), False):
            bb_gate = "pass" if _bool_metadata(m.get("bb_entry_gate_passed"), False) else "blocked"

        open_positions = self.broker.open_positions()
        same_pair = any(p.pair == candle.pair for p in open_positions)
        
        # Log to Counter for late chase/false breakout blocks
        if "false_breakout" in rejection_reason:
            self._entry_type_counts["false_breakout_blocked"] = self._entry_type_counts.get("false_breakout_blocked", 0) + 1
        elif "late_chase" in rejection_reason:
            self._entry_type_counts["late_chase_blocked"] = self._entry_type_counts.get("late_chase_blocked", 0) + 1

        signal_time_ms = int(signal.timestamp_ms or candle.close_time_ms)
        candle_dt = datetime.fromtimestamp(signal_time_ms / 1000, tz=timezone.utc)
        diag = {
            "pair": candle.pair,
            "time": candle_dt.strftime("%H:%M:%S"),
            "candle_time": candle_dt.isoformat(),
            "timestamp_ms": signal_time_ms,
            "side": signal.direction.value if signal.direction else "none",
            "entry_type": m.get("entry_type") or "none",
            "profile": m.get("pair_profile_label") or m.get("pair_profile") or "",
            "decision": decision_str,
            "reason": rejection_reason,
            "volume_ratio": f"{vol_ratio:.2f}",
            "body_ratio": f"{body_ratio:.2f}",
            "extension_atr": f"{m.get('execution_extension_atr') or m.get('extension_atr') or 0:.2f}",
            "rsi": _fmt_diag_decimal(m.get("rsi"), places=2),
            "macd_histogram": _fmt_diag_decimal(m.get("macd_histogram"), places=6),
            "bb_position": bb_position_text,
            "bb_gate": bb_gate,
            "breakout_age": m.get("breakout_age") or 0,
        }
        self._upsert_recent_diagnostic(diag)

        self._log_audit([
            datetime.fromtimestamp(candle.close_time_ms / 1000, tz=timezone.utc).isoformat(),
            candle.pair,
            self.settings.strategy_interval,
            candle.interval,
            str(candle.open), str(candle.high), str(candle.low), str(candle.close), str(candle.volume),
            str(m.get("previous_parent_high") or 0),
            str(m.get("previous_parent_low") or 0),
            signal.direction.value if signal.direction else "none",
            m.get("entry_type") or "none",
            decision_str,
            rejection_reason,
            f"{vol_ratio:.2f}",
            f"{body_ratio:.2f}",
            f"{cp_ratio:.2f}",
            f"{wick_ratio:.2f}",
            f"{m.get('ema_fast') or 0:.4f}",
            f"{m.get('atr') or 0:.4f}",
            f"{m.get('execution_extension_atr') or m.get('extension_atr') or 0:.2f}",
            m.get("breakout_age") or 0,
            0, # consecutive_impulse_candles - placeholder
            "none", # volume_fade_state - placeholder
            "passed" if "false_breakout" not in rejection_reason else "failed",
            "yes" if "late_chase" in rejection_reason else "no",
            "neutral", # higher_context_state - placeholder
            "yes" if decision_obj and decision_obj.approved else "no",
            decision_obj.reason if decision_obj and not decision_obj.approved else "none",
            len(open_positions),
            "yes" if same_pair else "no",
            len(open_positions)
        ])        

    def _upsert_recent_diagnostic(self, diag: dict[str, Any]) -> None:
        key = (
            diag.get("pair"),
            diag.get("timestamp_ms"),
            diag.get("entry_type"),
        )
        for index in range(len(self._recent_diagnostics) - 1, -1, -1):
            existing = self._recent_diagnostics[index]
            existing_key = (
                existing.get("pair"),
                existing.get("timestamp_ms"),
                existing.get("entry_type"),
            )
            if existing_key == key:
                self._recent_diagnostics[index] = diag
                return
        self._recent_diagnostics.append(diag)

    def _positions_payload(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for p in self.broker.open_positions():
            metadata = p.metadata if isinstance(p.metadata, dict) else {}
            mark_price = self._latest_mark_price_for_position(p.pair, p.entry_price)
            active_stop = (
                metadata.get("bb_trail_stop")
                or metadata.get("atr_stop_loss")
                or metadata.get("last_trailing_stop")
                or (p.stop_loss if _position_stop_management_active(metadata) else None)
            )
            stop_type = _position_stop_type(metadata)
            tp_suppressed = _position_take_profit_suppressed(metadata)
            rows.append(
                {
                    "pair": p.pair,
                    "direction": p.direction.value,
                    "quantity": str(p.quantity),
                    "entry_price": str(p.entry_price),
                    "mark_price": str(mark_price),
                    "unrealized_pnl": str(p.unrealized_pnl(mark_price)),
                    "notional": str(p.margin_notional(mark_price)),
                    "notional_currency": self.settings.futures_margin_currency,
                    "pnl_currency": self.settings.futures_margin_currency,
                    "price_quote_currency": self.settings.price_quote_currency,
                    "quote_to_margin_rate": str(p.quote_to_margin_rate),
                    "quantity_unit": _quantity_unit(p.pair),
                    "risk_percent_used": str(metadata.get("risk_percent_used") or ""),
                    "planned_risk_amount": str(metadata.get("planned_risk_amount") or ""),
                    "required_margin": str(metadata.get("required_margin") or ""),
                    "strategy": p.strategy_name,
                    "stop_loss": str(p.stop_loss) if p.stop_loss else None,
                    "take_profit": str(p.take_profit) if p.take_profit else None,
                    "active_trailing_stop": str(active_stop) if active_stop else None,
                    "take_profit_suppressed_by_trailing": tp_suppressed,
                    "atr_best_price": str(metadata.get("atr_best_price") or ""),
                    "stop_type": stop_type,
                    "atr_latest": str(metadata.get("atr_latest") or ""),
                    "trailing_stop_active": _bool_metadata(metadata.get("trailing_stop_active"), False),
                    "atr_dynamic_exit_active": _bool_metadata(metadata.get("atr_dynamic_exit_active"), False),
                    "profit_lock_enabled": _bool_metadata(metadata.get("profit_lock_enabled"), False),
                    "atr_trailing_enabled": _bool_metadata(metadata.get("atr_trailing_enabled"), False),
                    "bb_trail_enabled": _bool_metadata(metadata.get("bb_trail_enabled"), False),
                    "bb_trail_active": _bool_metadata(metadata.get("bb_trail_active"), False),
                    "bb_trail_stage": str(metadata.get("bb_trail_stage") or ""),
                    "bb_trail_stop": str(metadata.get("bb_trail_stop") or ""),
                    "bb_mid": str(metadata.get("bb_mid") or ""),
                    "bb_upper": str(metadata.get("bb_upper") or ""),
                    "bb_lower": str(metadata.get("bb_lower") or ""),
                    "bb_buffer_atr": str(metadata.get("bb_buffer_atr") or ""),
                    "bb_entry_band_position": str(metadata.get("bb_entry_band_position") or ""),
                    "bb_entry_gate_passed": _bool_metadata(metadata.get("bb_entry_gate_passed"), False),
                    "bb_trail_partial_close_pct": str(metadata.get("bb_trail_partial_close_pct") or ""),
                    "tp_is_partial_close": _bool_metadata(metadata.get("tp_is_partial_close"), False),
                    "bb_partial_close_executed": _bool_metadata(metadata.get("bb_partial_close_executed"), False),
                    "execution_mode": metadata.get("execution_mode") or self.execution_mode,
                    "live_dry_run": _bool_metadata(metadata.get("live_dry_run"), self.live_dry_run),
                    "live_position_id": str(metadata.get("live_position_id") or ""),
                    "live_pilot": _bool_metadata(metadata.get("live_pilot"), False),
                }
            )
        return rows

    def _live_positions_payload(self) -> list[dict[str, Any]]:
        return list(self._live_positions.values())

    def _live_orders_payload(self) -> list[dict[str, Any]]:
        return list(self._live_orders)

    def _live_sync_payload(self) -> dict[str, Any]:
        return {
            **self._live_sync_status,
            "execution_mode": self.execution_mode,
            "live_dry_run": self.live_dry_run,
            "recent_reports": self._live_execution_reports[-10:],
        }

    def _latest_mark_price_for_position(self, pair: str, fallback: Decimal) -> Decimal:
        mark_price = self.broker.mark_price_for(pair)
        if mark_price is not None:
            return mark_price

        for series_map in (self.execution_series, self.series):
            series = series_map.get(pair)
            latest = series.latest() if series is not None else None
            if latest is not None:
                return latest.close

        candles = self.candle_history.get(pair) or []
        if candles:
            latest_close = candles[-1].get("close")
            if latest_close is not None:
                return Decimal(str(latest_close))

        return fallback

    def _mark_prices_for_open_positions(self, candle: OHLCVCandle) -> dict[str, Decimal]:
        prices: dict[str, Decimal] = {}
        for position in self.broker.open_positions():
            if position.pair == candle.pair:
                prices[position.pair] = candle.close
            else:
                prices[position.pair] = self._latest_mark_price_for_position(
                    position.pair,
                    position.entry_price,
                )
        return prices

    def _force_close_price_for_position(
        self,
        position: PaperPosition,
        candle: OHLCVCandle,
    ) -> Decimal:
        if position.pair == candle.pair:
            return candle.close
        return self._latest_mark_price_for_position(position.pair, position.entry_price)

    def _force_close_all_positions(
        self,
        candle: OHLCVCandle,
        *,
        reason: str,
    ) -> list[PaperExecutionReport]:
        reports: list[PaperExecutionReport] = []
        for pos in list(self.broker.open_positions()):
            action = (
                SignalAction.EXIT_LONG
                if pos.direction == SignalDirection.LONG
                else SignalAction.EXIT_SHORT
            )
            exit_price = self._force_close_price_for_position(pos, candle)
            exit_signal = StrategySignal(
                strategy_name=pos.strategy_name,
                pair=pos.pair,
                interval=candle.interval,
                action=action,
                confidence=Decimal("1"),
                direction=pos.direction,
                timestamp_ms=candle.close_time_ms,
                entry_price=exit_price,
                reason=reason,
                metadata={
                    "force_close_trigger_pair": candle.pair,
                    "force_close_price_pair": pos.pair,
                    "live_position_id": str(pos.metadata.get("live_position_id") or ""),
                },
            )
            exit_decision = RiskDecision(
                approved=True,
                reason=reason,
                signal=exit_signal,
            )
            if self._live_execution_enabled:
                live_report = self._execute_live_decision(exit_decision)
                if not live_report.accepted:
                    continue
                exit_decision = self._with_live_execution_metadata(exit_decision, live_report)
            report = self.broker.execute_decision(
                exit_decision,
                market_price=exit_price,
                timestamp_ms=candle.close_time_ms,
            )
            reports.append(report)
        return reports

    def _candle_payload(self, candle: OHLCVCandle) -> dict[str, Any]:
        return {
            "pair": candle.pair,
            "interval": candle.interval,
            "t": candle.close_time_ms,
            "open_time_ms": candle.open_time_ms,
            "open": str(candle.open),
            "high": str(candle.high),
            "low": str(candle.low),
            "close": str(candle.close),
            "volume": str(candle.volume),
        }

    def _seed_candle_history(self, pair: str, series: CandleSeries) -> None:
        self.candle_history[pair] = [
            self._candle_payload(candle) for candle in list(series)[-120:]
        ]

    def _record_live_candle(self, candle: OHLCVCandle) -> None:
        preferred_interval = (
            self.settings.execution_interval
            if self.settings.paper_intrabar_enabled
            else self._current_interval
        )
        if preferred_interval != "unknown" and candle.interval != preferred_interval:
            return

        history = self.candle_history.setdefault(candle.pair, [])
        payload = self._candle_payload(candle)
        if history and history[-1].get("t") == payload["t"]:
            history[-1] = payload
        else:
            history.append(payload)
        if len(history) > 240:
            del history[:-240]

    def _candles_payload(self) -> dict[str, Any]:
        active_pairs = self._watchlist_snapshot()
        open_pairs = [position.pair for position in self.broker.open_positions()]
        payload_pairs = self._dedupe_pairs([*active_pairs, *open_pairs])
        chart_payloads: dict[str, dict[str, Any]] = {}
        for pair in payload_pairs:
            candles_for_pair = self.candle_history.get(pair, [])[-120:]
            if not candles_for_pair:
                continue
            chart_payloads[pair] = {
                "pair": pair,
                "interval": candles_for_pair[-1].get("interval", ""),
                "candles": candles_for_pair,
            }

        chart_pair = ""
        for position in self.broker.open_positions():
            if position.pair in self.candle_history:
                chart_pair = position.pair
                break
        if not chart_pair:
            for pair in active_pairs:
                if pair in self.candle_history:
                    chart_pair = pair
                    break
        if not chart_pair and self.candle_history:
            chart_pair = next(iter(self.candle_history))

        candles = self.candle_history.get(chart_pair, [])[-120:] if chart_pair else []
        interval = candles[-1].get("interval", "") if candles else ""
        return {
            "pair": chart_pair,
            "interval": interval,
            "candles": candles,
            "pairs": chart_payloads,
        }

    def _publish_live_snapshot(
        self,
        *,
        interval: str | None = None,
        last_updated: str | None = None,
        error: str | None = None,
    ) -> None:
        snapshot = self.broker.snapshot()
        drawdown = self._equity_drawdown_summary(snapshot.equity)
        watchlist = self._watchlist_snapshot()
        visible_override_pairs = {
            *watchlist,
            *(position.pair for position in self.broker.open_positions()),
        }
        
        # Build scanned pairs status
        scanned: dict[str, str] = {}
        pair_profiles: dict[str, dict[str, Any]] = {}
        for pair in watchlist:
            pair_profiles[pair] = pair_profile_for(pair).metadata()
            if pair in self.series:
                latest = self.series[pair].latest()
                if latest:
                    ts = time.strftime('%H:%M:%S', time.gmtime(latest.close_time_ms / 1000))
                    scanned[pair] = f"Closed: {ts} @ {latest.close}"
                else:
                    scanned[pair] = "Waiting for data..."
            else:
                scanned[pair] = "Not initialized"

        update: dict[str, Any] = {
            "execution_mode": self.execution_mode,
            "live_dry_run": self.live_dry_run,
            "candle_count": self.candle_count,
            "strategy_interval": self.settings.strategy_interval,
            "execution_interval": (
                self.settings.execution_interval
                if self.settings.paper_intrabar_enabled
                else ""
            ),
            "equity": str(snapshot.equity),
            "realized_pnl": str(self.broker.realized_pnl),
            "unrealized_pnl": str(snapshot.unrealized_pnl),
            "net_realized_pnl": str(self.broker.realized_pnl - self.broker.fees_paid),
            "fees_paid": str(self.broker.fees_paid),
            "open_notional": str(snapshot.open_notional),
            "return_abs": str(snapshot.equity - snapshot.starting_equity),
            "return_pct": str(
                ((snapshot.equity - snapshot.starting_equity) / snapshot.starting_equity)
                * Decimal("100")
                if snapshot.starting_equity > 0
                else Decimal("0")
            ),
            "max_drawdown_pct": str(drawdown["max_drawdown_pct"]),
            "peak_equity": str(drawdown["peak_equity"]),
            "open_positions": len(self.broker.positions),
            "total_fills": len(self.broker.fills),
            "positions_json": json.dumps(to_jsonable(self._positions_payload())),
            "live_positions_json": json.dumps(to_jsonable(self._live_positions_payload())),
            "live_orders_json": json.dumps(to_jsonable(self._live_orders_payload())),
            "live_sync_json": json.dumps(to_jsonable(self._live_sync_payload())),
            "equity_history_json": json.dumps(to_jsonable(self.equity_history[-500:])),
            "candles_json": json.dumps(to_jsonable(self._candles_payload())),
            "watchlist": watchlist,
            "scanned_pairs": scanned,
            "pair_profiles": pair_profiles,
            "pair_overrides": {
                pair: settings
                for pair, settings in self._pair_overrides.items()
                if pair in visible_override_pairs
            },
            "entry_type_counts": self._entry_type_counts,
            "recent_diagnostics": list(self._recent_diagnostics),
            "session_started_at": self._session_started_at,
            "initial_equity": str(snapshot.initial_equity),
            "total_equity": str(snapshot.total_equity),
            "tradable_equity": str(snapshot.tradable_equity),
            "tradable_base": str(snapshot.tradable_base),
            "locked_profit": str(snapshot.locked_profit),
            "unlocked_profit": str(snapshot.unlocked_profit),
            "daily_drawdown": str(drawdown),
            "daily_loss_from_tradable_base": str(snapshot.daily_loss_from_tradable_base),
            "profit_lock_enabled": snapshot.profit_lock_enabled,
            "protected_profit_override_enabled": snapshot.protected_profit_override_enabled,
            "live_max_order_notional": str(self.settings.risk.live_max_order_notional),
            "live_max_margin_per_order": str(self.settings.risk.live_max_margin_per_order),
            "live_require_stop_loss": self.settings.risk.live_require_stop_loss,
            "live_kill_switch": self.settings.risk.live_kill_switch,
        }
        if interval is not None:
            update["interval"] = interval
        if last_updated is not None:
            update["last_updated"] = last_updated
        if error is not None:
            update["error"] = error
        _update_live_state(**update)

    def _equity_drawdown_summary(self, current_equity: Decimal) -> dict[str, Decimal]:
        values = [self.broker.starting_equity]
        for point in self.equity_history:
            if isinstance(point, dict):
                value = _decimal_metadata(point.get("equity"), Decimal("0"))
                if value > 0:
                    values.append(value)
        if current_equity > 0:
            values.append(current_equity)

        peak = Decimal("0")
        max_drawdown = Decimal("0")
        for value in values:
            if value > peak:
                peak = value
            if peak > 0:
                drawdown = ((peak - value) / peak) * Decimal("100")
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
        return {
            "peak_equity": peak,
            "max_drawdown_pct": max_drawdown,
        }

    def _load_session(self) -> None:
        saved = self.session_store.load_session()
        if saved:
            self.candle_count = int(saved.get("candle_count", 0))
            self.equity_history = saved.get("equity_history", [])
            raw_candle_history = saved.get("candle_history")
            if isinstance(raw_candle_history, dict):
                self.candle_history = {
                    str(pair): list(values)[-240:]
                    for pair, values in raw_candle_history.items()
                    if isinstance(values, list)
                }
            self._position_entry_candle = {
                k: int(v) for k, v in saved.get("position_entry_candle", {}).items()
                if isinstance(saved.get("position_entry_candle"), dict)
            }
            self._position_entry_prices = {
                k: Decimal(str(v)) for k, v in saved.get("position_entry_prices", {}).items()
                if isinstance(saved.get("position_entry_prices"), dict)
            }
            
            # Robust loading for parent candle state (could be int in old sessions)
            raw_entries = saved.get("entries_this_parent_candle")
            if isinstance(raw_entries, dict):
                self._entries_this_parent_candle = {k: int(v) for k, v in raw_entries.items()}
            elif isinstance(raw_entries, (int, float)):
                # Default to first pair if known, or leave as empty dict (will be initialized on first candle)
                pass

            raw_open_ms = saved.get("current_parent_open_ms")
            if isinstance(raw_open_ms, dict):
                self._current_parent_open_ms = {k: int(v) for k, v in raw_open_ms.items()}
            elif isinstance(raw_open_ms, (int, float)):
                pass

            raw_pair_cooldown = saved.get("pair_cooldown_until_ms")
            if isinstance(raw_pair_cooldown, dict):
                self._pair_cooldown_until_ms = {
                    str(pair): int(value)
                    for pair, value in raw_pair_cooldown.items()
                    if value is not None
                }

            raw_direction_cooldown = saved.get("same_direction_cooldown_until_ms")
            if isinstance(raw_direction_cooldown, dict):
                restored: dict[tuple[str, str], int] = {}
                for raw_key, value in raw_direction_cooldown.items():
                    if value is None:
                        continue
                    key = str(raw_key)
                    if "|" not in key:
                        continue
                    pair, direction = key.rsplit("|", 1)
                    if pair and direction:
                        restored[(pair, direction)] = int(value)
                self._same_direction_cooldown_until_ms = restored

            raw_reversal_cooldown = saved.get("same_direction_reversal_cooldown_until_ms")
            if isinstance(raw_reversal_cooldown, dict):
                restored_reversal: dict[tuple[str, str], int] = {}
                for raw_key, value in raw_reversal_cooldown.items():
                    if value is None:
                        continue
                    key = str(raw_key)
                    if "|" not in key:
                        continue
                    pair, direction = key.rsplit("|", 1)
                    if pair and direction:
                        restored_reversal[(pair, direction)] = int(value)
                self._same_direction_reversal_cooldown_until_ms = restored_reversal

            raw_post_profit_reentry = saved.get("post_profit_reentry_state")
            if isinstance(raw_post_profit_reentry, dict):
                self._post_profit_reentry_state = {
                    str(pair): dict(state)
                    for pair, state in raw_post_profit_reentry.items()
                    if isinstance(state, dict)
                }

            raw_pair_pnls = saved.get("pair_recent_net_pnls")
            if isinstance(raw_pair_pnls, dict):
                restored_pnls: dict[str, list[Decimal]] = {}
                lookback = max(self.settings.risk.pair_loss_lookback, 1)
                for pair, values in raw_pair_pnls.items():
                    if not isinstance(values, list):
                        continue
                    pnls: list[Decimal] = []
                    for value in values[-lookback:]:
                        try:
                            pnls.append(Decimal(str(value)))
                        except Exception:
                            continue
                    restored_pnls[str(pair)] = pnls
                self._pair_recent_net_pnls = restored_pnls

            raw_pair_overrides = saved.get("pair_overrides")
            if isinstance(raw_pair_overrides, dict):
                self.update_pair_overrides(raw_pair_overrides, publish=False)

            self._global_loss_cooldown_until_ms = int(
                saved.get("global_loss_cooldown_until_ms", 0) or 0
            )
            self._consecutive_losing_trades = int(
                saved.get("consecutive_losing_trades", 0) or 0
            )
                
            self.logger.info("Restored paper session: candle_count=%d", self.candle_count)

    def _save_session(self) -> None:
        # Task 3: Autosave
        snapshot = self.broker.snapshot()
        state = {
            "candle_count": self.candle_count,
            "execution_mode": self.execution_mode,
            "live_dry_run": self.live_dry_run,
            "live_sync_status": self._live_sync_status,
            "watchlist": self._watchlist_snapshot(),
            "equity_history": self.equity_history,
            "candle_history": self.candle_history,
            "position_entry_candle": self._position_entry_candle,
            "position_entry_prices": {k: str(v) for k, v in self._position_entry_prices.items()},
            "entries_this_parent_candle": self._entries_this_parent_candle,
            "current_parent_open_ms": self._current_parent_open_ms,
            "pair_cooldown_until_ms": self._pair_cooldown_until_ms,
            "same_direction_cooldown_until_ms": {
                f"{pair}|{direction}": value
                for (pair, direction), value in self._same_direction_cooldown_until_ms.items()
            },
            "same_direction_reversal_cooldown_until_ms": {
                f"{pair}|{direction}": value
                for (pair, direction), value in self._same_direction_reversal_cooldown_until_ms.items()
            },
            "post_profit_reentry_state": self._post_profit_reentry_state,
            "pair_recent_net_pnls": {
                pair: [str(value) for value in values]
                for pair, values in self._pair_recent_net_pnls.items()
            },
            "pair_overrides": self._pair_overrides,
            "global_loss_cooldown_until_ms": self._global_loss_cooldown_until_ms,
            "consecutive_losing_trades": self._consecutive_losing_trades,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            # Mirror some broker state for easy JSON access if needed
            "realized_pnl": str(self.broker.realized_pnl),
            "fees_paid": str(self.broker.fees_paid),
            "equity": str(snapshot.equity),
        }
        try:
            self.session_store.save_session(state)
        except Exception as exc:
            self.logger.exception("Failed to save paper session snapshot.")
            _update_live_state(
                error=f"Failed to save paper session snapshot: {exc}",
                last_updated=datetime.now(timezone.utc).isoformat(),
            )

    def _dedupe_pairs(self, pairs: list[str] | tuple[str, ...]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for pair in pairs:
            normalized = str(pair or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    def update_pair_overrides(
        self,
        overrides: dict[str, dict[str, Any]] | None,
        *,
        publish: bool = True,
    ) -> dict[str, dict[str, Any]]:
        if not overrides:
            if publish:
                self._publish_live_snapshot(last_updated="pair settings updated")
            return dict(self._pair_overrides)

        normalized = self._normalize_pair_overrides(overrides)
        with self._watchlist_lock:
            for pair, settings in normalized.items():
                if settings:
                    self._pair_overrides[pair] = settings
                    self._apply_pair_override_to_open_position(pair, settings)
                else:
                    self._pair_overrides.pop(pair, None)
        if publish:
            self._save_session()
            self._publish_live_snapshot(last_updated="pair settings updated")
        return dict(self._pair_overrides)

    def _apply_pair_override_to_open_position(
        self,
        pair: str,
        settings: dict[str, Any],
    ) -> None:
        position = self.broker.positions.get(pair)
        if position is None:
            return
        metadata = dict(position.metadata)
        if "trailing_stop_enabled" in settings:
            metadata["trailing_stop_enabled"] = settings["trailing_stop_enabled"]
        if "atr_dynamic_exits_enabled" in settings:
            enabled = settings["atr_dynamic_exits_enabled"]
            metadata["atr_dynamic_exits_enabled"] = enabled
            metadata["atr_stop_enabled"] = enabled
            metadata["atr_take_profit_enabled"] = enabled
            metadata["atr_trailing_enabled"] = enabled
        if "profit_lock_enabled" in settings:
            metadata["profit_lock_enabled"] = settings["profit_lock_enabled"]
        if "bb_trail_enabled" in settings:
            metadata["bb_trail_enabled"] = settings["bb_trail_enabled"]
        self.broker.positions[pair] = replace(position, metadata=metadata)
        self.broker._save_state()

    def update_runtime_settings(
        self,
        *,
        strategy_interval: str | None = None,
        execution_interval: str | None = None,
        paper_intrabar_enabled: bool | None = None,
        use_partial_parent_candle: bool | None = None,
        max_entries_per_parent_candle: int | None = None,
        paper_leverage: Decimal | None = None,
        risk_updates: dict[str, Any] | None = None,
        publish: bool = True,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        clean_risk_updates = {
            key: value
            for key, value in (risk_updates or {}).items()
            if hasattr(self.settings.risk, key)
        }
        if strategy_interval:
            updates["strategy_interval"] = strategy_interval
        if execution_interval:
            updates["execution_interval"] = execution_interval
        if paper_intrabar_enabled is not None:
            updates["paper_intrabar_enabled"] = paper_intrabar_enabled
        if use_partial_parent_candle is not None:
            updates["use_partial_parent_candle"] = use_partial_parent_candle
        if max_entries_per_parent_candle is not None:
            updates["max_entries_per_parent_candle"] = max(1, int(max_entries_per_parent_candle))
        if paper_leverage is not None and paper_leverage > 0:
            updates["paper_leverage"] = paper_leverage
        if not updates and not clean_risk_updates:
            return {
                "updated": False,
                "interval": self._current_interval,
                "settings": {},
                "risk_settings": {},
            }

        old_strategy_interval = self._current_interval
        old_intrabar = self.settings.paper_intrabar_enabled
        old_execution_interval = self.settings.execution_interval
        risk_settings = (
            replace(self.settings.risk, **clean_risk_updates)
            if clean_risk_updates
            else self.settings.risk
        )
        self.settings = replace(self.settings, **updates, risk=risk_settings)
        new_strategy_interval = (
            self.settings.strategy_interval
            if self.settings.paper_intrabar_enabled
            else updates.get("strategy_interval", self._current_interval)
        )
        if new_strategy_interval:
            self._current_interval = str(new_strategy_interval)

        interval_changed = (
            self._current_interval != old_strategy_interval
            or self.settings.paper_intrabar_enabled != old_intrabar
            or self.settings.execution_interval != old_execution_interval
        )
        subscription_pairs = self._subscription_pairs_for_current_state()
        if interval_changed:
            with self._watchlist_lock:
                self._subscriptions = []
            self._replace_subscription_snapshot(subscription_pairs, self._current_interval)
            if subscription_pairs:
                threading.Thread(
                    target=self._activate_watchlist_pairs_safely,
                    args=(subscription_pairs, self._current_interval),
                    daemon=True,
                    name="paper-runtime-settings-update",
                ).start()
        elif self._ws_client is not None:
            self._ws_client.subscribe(self._subscriptions_snapshot())

        if publish:
            self._save_session()
            self._publish_live_snapshot(
                interval=self._current_interval,
                last_updated="settings updated",
                error="",
            )
        return {
            "updated": True,
            "interval": self._current_interval,
            "settings": updates,
            "risk_settings": clean_risk_updates,
            "subscriptions_refreshed": interval_changed,
        }

    def _subscription_pairs_for_current_state(self) -> list[str]:
        watchlist = self._watchlist_snapshot()
        open_position_pairs = [
            position.pair
            for position in self.broker.open_positions()
            if position.pair not in watchlist
        ]
        return self._dedupe_pairs([*watchlist, *open_position_pairs])

    def _normalize_pair_overrides(
        self,
        overrides: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        allowed = {
            "strategy",
            "leverage",
            "risk_pct",
            "max_daily_loss_pct",
            "max_open_positions",
            "max_margin_usage_pct",
            "allow_multi_pair_positions",
            "allow_same_pair_pyramiding",
            "trailing_stop_enabled",
            "atr_dynamic_exits_enabled",
            "profit_lock_enabled",
            "bb_trail_enabled",
            "max_entries_per_parent_candle",
        }
        normalized: dict[str, dict[str, Any]] = {}
        for raw_pair, raw_settings in overrides.items():
            pair = str(raw_pair or "").strip()
            if not pair or not isinstance(raw_settings, dict):
                continue
            clean: dict[str, Any] = {}
            for key in allowed:
                if key not in raw_settings:
                    continue
                value = raw_settings[key]
                if value is None or value == "":
                    continue
                if key == "strategy":
                    strategy = str(value).strip().lower()
                    if strategy in STRATEGY_CHOICES and strategy != "all":
                        clean[key] = strategy
                    continue
                if key in {
                    "allow_multi_pair_positions",
                    "allow_same_pair_pyramiding",
                    "trailing_stop_enabled",
                    "atr_dynamic_exits_enabled",
                    "profit_lock_enabled",
                    "bb_trail_enabled",
                }:
                    clean[key] = _bool_metadata(value)
                    continue
                if key in {"max_open_positions", "max_entries_per_parent_candle"}:
                    try:
                        clean[key] = max(1, int(value))
                    except Exception:
                        continue
                    continue
                decimal_value = _decimal_metadata(value, Decimal("-1"))
                if decimal_value >= 0:
                    clean[key] = decimal_value
            normalized[pair] = clean
        return normalized

    def _pair_override(self, pair: str) -> dict[str, Any]:
        return self._pair_overrides.get(pair, {})

    def _risk_settings_for_pair(self, pair: str) -> RiskSettings:
        override = self._pair_override(pair)
        updates: dict[str, Any] = {}
        if "risk_pct" in override:
            updates["max_risk_per_trade_pct"] = override["risk_pct"]
        if "max_daily_loss_pct" in override:
            updates["max_daily_loss_pct"] = override["max_daily_loss_pct"]
        if "max_open_positions" in override:
            updates["max_open_positions"] = override["max_open_positions"]
            updates["max_open_positions_per_pair"] = override["max_open_positions"]
        if "max_margin_usage_pct" in override:
            updates["max_margin_usage_pct"] = override["max_margin_usage_pct"]
        if "allow_multi_pair_positions" in override:
            updates["allow_multi_pair_positions"] = override["allow_multi_pair_positions"]
        if "allow_same_pair_pyramiding" in override:
            updates["allow_same_pair_pyramiding"] = override["allow_same_pair_pyramiding"]
        if "trailing_stop_enabled" in override:
            updates["trailing_stop_enabled"] = override["trailing_stop_enabled"]
        if "atr_dynamic_exits_enabled" in override:
            enabled = override["atr_dynamic_exits_enabled"]
            updates["atr_stop_enabled"] = enabled
            updates["atr_take_profit_enabled"] = enabled
            updates["atr_trailing_enabled"] = enabled
        if "profit_lock_enabled" in override:
            updates["profit_lock_enabled"] = override["profit_lock_enabled"]
        if "bb_trail_enabled" in override:
            updates["bb_trail_enabled"] = override["bb_trail_enabled"]
        return replace(self.settings.risk, **updates) if updates else self.settings.risk

    def _with_pair_runtime_metadata(
        self,
        signal: StrategySignal,
        pair_risk: RiskSettings,
    ) -> StrategySignal:
        """Stamp entry signals with the pair controls that the broker must honor."""
        if not is_entry_signal(signal):
            return signal
        metadata = {
            **signal.metadata,
            "paper_pair_strategy": self._strategy_name_for_pair(signal.pair),
            "paper_pair_leverage": self._paper_leverage_for_pair(signal.pair),
            "paper_pair_override_applied": bool(self._pair_override(signal.pair)),
            "trailing_stop_enabled": pair_risk.trailing_stop_enabled,
            "trailing_stop_activation_pct": pair_risk.trailing_stop_activation_pct,
            "trailing_stop_distance_pct": pair_risk.trailing_stop_distance_pct,
            "atr_dynamic_exits_enabled": (
                pair_risk.atr_stop_enabled
                or pair_risk.atr_take_profit_enabled
                or pair_risk.atr_trailing_enabled
            ),
            "atr_stop_enabled": pair_risk.atr_stop_enabled,
            "atr_take_profit_enabled": pair_risk.atr_take_profit_enabled,
            "atr_trailing_enabled": pair_risk.atr_trailing_enabled,
            "profit_lock_enabled": pair_risk.profit_lock_enabled,
            "profit_giveback_guard_enabled": pair_risk.profit_giveback_guard_enabled,
            "bb_trail_enabled": pair_risk.bb_trail_enabled,
            "bb_trail_observe_only": pair_risk.bb_trail_observe_only,
        }
        return replace(signal, metadata=metadata)

    def _paper_leverage_for_pair(self, pair: str) -> Decimal:
        override = self._pair_override(pair)
        leverage = _decimal_metadata(override.get("leverage"), self.settings.paper_leverage)
        return leverage if leverage > 0 else self.settings.paper_leverage

    def _max_entries_per_parent_candle_for_pair(self, pair: str) -> int:
        override = self._pair_override(pair)
        value = override.get("max_entries_per_parent_candle")
        try:
            return max(1, int(value))
        except Exception:
            return max(1, int(self.settings.max_entries_per_parent_candle))

    def _strategy_name_for_pair(self, pair: str) -> str:
        override = self._pair_override(pair)
        strategy = str(override.get("strategy") or "").strip().lower()
        if strategy in STRATEGY_CHOICES and strategy != "all":
            return strategy
        return self.strategy_engine.strategies[0].name if self.strategy_engine.strategies else "unknown"

    def _strategy_engine_for_pair(self, pair: str) -> StrategyEngine:
        strategy = self._strategy_name_for_pair(pair)
        current_strategy = (
            self.strategy_engine.strategies[0].name
            if self.strategy_engine.strategies
            else ""
        )
        if strategy == current_strategy or strategy not in STRATEGY_CHOICES:
            return self.strategy_engine
        if strategy not in self._strategy_engines_by_name:
            self._strategy_engines_by_name[strategy] = strategy_engine_for_name(strategy)
        return self._strategy_engines_by_name[strategy]

    def _strategy_uses_closed_parent_entries_only(self, pair: str) -> bool:
        return (
            self._strategy_name_for_pair(pair) in {"fib_ma_pullback"}
            and not self._fib_allows_execution_trigger_entries(pair)
        )

    def _fib_allows_execution_trigger_entries(self, pair: str) -> bool:
        if not self.settings.paper_intrabar_enabled:
            return False
        if self._strategy_name_for_pair(pair) != "fib_ma_pullback":
            return False
        try:
            strategy_ms = interval_to_ms(self.settings.strategy_interval)
            execution_ms = interval_to_ms(self.settings.execution_interval)
        except ValueError:
            return False
        return (
            strategy_ms >= 15 * 60_000
            and execution_ms >= 5 * 60_000
            and execution_ms < strategy_ms
        )

    def _should_evaluate_entries_on_candle(self, pair: str, candle: OHLCVCandle) -> bool:
        if not self.settings.paper_intrabar_enabled:
            return True
        if candle.interval != self.settings.execution_interval:
            return True
        if self.settings.execution_interval == self.settings.strategy_interval:
            return True
        if self._fib_allows_execution_trigger_entries(pair):
            return True
        if not self._strategy_uses_closed_parent_entries_only(pair):
            return True

        series = self.series.get(pair)
        latest_parent = series.latest() if series is not None else None
        if latest_parent is None:
            return False
        if latest_parent.close_time_ms > candle.close_time_ms:
            return False

        execution_ms = interval_to_ms(self.settings.execution_interval)
        max_processing_lag_ms = max(execution_ms * 2, 120_000)
        if candle.close_time_ms - latest_parent.close_time_ms > max_processing_lag_ms:
            return False

        last_eval_ms = self._last_closed_strategy_eval_ms.get(pair, 0)
        if latest_parent.close_time_ms <= last_eval_ms:
            return False

        self._last_closed_strategy_eval_ms[pair] = latest_parent.close_time_ms
        return True

    def _strategy_warmup_lookback_for_pair(self, pair: str) -> int:
        lookback = 200
        for strategy in self._strategy_engine_for_pair(pair).strategies:
            try:
                lookback = max(lookback, int(getattr(strategy, "warmup_lookback", 0)))
            except (TypeError, ValueError):
                continue
        return lookback

    def _watchlist_snapshot(self) -> list[str]:
        with self._watchlist_lock:
            return list(self._watchlist)

    def _watchlist_contains(self, pair: str) -> bool:
        with self._watchlist_lock:
            return pair in self._watchlist

    def _pair_active_for_processing(self, pair: str) -> bool:
        return self._watchlist_contains(pair) or pair in self.broker.positions

    def _subscriptions_snapshot(self) -> list[MarketSubscription]:
        with self._watchlist_lock:
            return list(self._subscriptions)

    def _strategy_interval_for_live_update(self) -> str:
        if self._current_interval != "unknown":
            return self._current_interval
        return self.settings.strategy_interval

    def _subscriptions_for_pair(
        self,
        pair: str,
        strategy_interval: str,
    ) -> list[MarketSubscription]:
        subscriptions = [
            MarketSubscription(futures_candle_channel(pair, strategy_interval), "candlestick")
        ]
        if (
            self.settings.paper_intrabar_enabled
            and strategy_interval != self.settings.execution_interval
        ):
            subscriptions.append(
                MarketSubscription(
                    futures_candle_channel(pair, self.settings.execution_interval),
                    "candlestick",
                )
            )
        subscriptions.extend(
            [
                MarketSubscription(futures_orderbook_channel(pair, 50), "depth-snapshot"),
                MarketSubscription(futures_orderbook_channel(pair, 50), "depth-update"),
            ]
        )
        return subscriptions

    def _subscriptions_for_pairs(
        self,
        pairs: list[str],
        strategy_interval: str,
    ) -> list[MarketSubscription]:
        subscriptions: list[MarketSubscription] = []
        seen: set[MarketSubscription] = set()
        for pair in pairs:
            for subscription in self._subscriptions_for_pair(pair, strategy_interval):
                if subscription in seen:
                    continue
                seen.add(subscription)
                subscriptions.append(subscription)
        return subscriptions

    def _remember_subscriptions(
        self,
        subscriptions: list[MarketSubscription],
    ) -> list[MarketSubscription]:
        with self._watchlist_lock:
            known = set(self._subscriptions)
            added: list[MarketSubscription] = []
            for subscription in subscriptions:
                if subscription in known:
                    continue
                known.add(subscription)
                self._subscriptions.append(subscription)
                added.append(subscription)
            return added

    def _replace_subscription_snapshot(
        self,
        pairs: list[str],
        strategy_interval: str,
    ) -> None:
        with self._watchlist_lock:
            self._subscriptions = self._subscriptions_for_pairs(pairs, strategy_interval)

    def _initialize_watchlist_pair(self, pair: str, strategy_interval: str) -> None:
        strategy_series = self.series.get(pair)
        strategy_latest = strategy_series.latest() if strategy_series is not None else None
        if (
            pair not in self.gap_guards
            or strategy_series is None
            or strategy_latest is None
            or strategy_latest.interval != strategy_interval
        ):
            self.gap_guards[pair] = CandleGapGuard(strategy_interval)
            self._warm_up(pair, strategy_interval)
        execution_series = self.execution_series.get(pair)
        execution_latest = execution_series.latest() if execution_series is not None else None
        if (
            self.settings.paper_intrabar_enabled
            and (
                pair not in self.execution_series
                or execution_latest is None
                or execution_latest.interval != self.settings.execution_interval
            )
        ):
            self._warm_up_execution(pair, self.settings.execution_interval)
        self._entries_this_parent_candle.setdefault(pair, 0)
        self._current_parent_open_ms.setdefault(pair, 0)

    def _activate_watchlist_pair(self, pair: str, strategy_interval: str) -> None:
        self._initialize_watchlist_pair(pair, strategy_interval)
        subscriptions = self._subscriptions_for_pair(pair, strategy_interval)
        new_subscriptions = self._remember_subscriptions(subscriptions)
        if self._ws_client is not None:
            self._ws_client.subscribe(new_subscriptions or subscriptions)
        self._save_session()
        self._publish_live_snapshot(last_updated=datetime.now(timezone.utc).isoformat(), error="")

    def _activate_watchlist_pairs_safely(
        self,
        pairs: list[str],
        strategy_interval: str,
    ) -> None:
        for pair in pairs:
            if self._stop_requested or not self._pair_active_for_processing(pair):
                continue
            try:
                self._activate_watchlist_pair(pair, strategy_interval)
            except Exception as exc:
                message = f"Failed to activate {pair} dynamically: {exc}"
                self.logger.exception(message)
                _update_live_state(
                    error=message,
                    last_updated=datetime.now(timezone.utc).isoformat(),
                )

    def add_pair_to_watchlist(
        self,
        pair: str,
        *,
        background: bool = True,
    ) -> dict[str, Any]:
        pair = str(pair or "").strip()
        if not pair:
            raise ValueError("No pair provided.")

        with self._watchlist_lock:
            if pair in self._watchlist:
                return {
                    "added": False,
                    "pair": pair,
                    "watchlist": list(self._watchlist),
                    "reason": f"{pair} already in watchlist",
                }
            self._watchlist.append(pair)
            watchlist = list(self._watchlist)

        strategy_interval = self._strategy_interval_for_live_update()
        _update_live_state(
            pair=", ".join(watchlist),
            watchlist=watchlist,
            last_updated=f"adding {pair}",
            error="",
        )
        self._publish_live_snapshot(last_updated=f"adding {pair}")

        if background:
            threading.Thread(
                target=self._activate_watchlist_pairs_safely,
                args=([pair], strategy_interval),
                daemon=True,
                name=f"paper-watchlist-add-{pair}",
            ).start()
        else:
            self._activate_watchlist_pair(pair, strategy_interval)

        return {
            "added": True,
            "pair": pair,
            "watchlist": watchlist,
            "status": "warming",
        }

    def update_watchlist(
        self,
        pairs: list[str],
        *,
        background: bool = True,
    ) -> dict[str, Any]:
        target = self._dedupe_pairs(pairs)
        if not target:
            raise ValueError("At least one pair is required in the running watchlist.")

        strategy_interval = self._strategy_interval_for_live_update()
        with self._watchlist_lock:
            previous = list(self._watchlist)
            self._watchlist = target
            added = [pair for pair in target if pair not in previous]
            removed = [pair for pair in previous if pair not in target]

        open_position_pairs = [
            position.pair
            for position in self.broker.open_positions()
            if position.pair not in target
        ]
        subscription_pairs = self._dedupe_pairs([*target, *open_position_pairs])
        needs_init = [
            pair
            for pair in subscription_pairs
            if pair in added
            or pair not in self.series
            or (
                self.settings.paper_intrabar_enabled
                and pair not in self.execution_series
            )
        ]
        subscribed_existing = [pair for pair in subscription_pairs if pair not in needs_init]
        self._replace_subscription_snapshot(subscribed_existing, strategy_interval)

        _update_live_state(
            pair=", ".join(target),
            watchlist=target,
            last_updated="watchlist updated",
            error="",
        )
        self._publish_live_snapshot(last_updated="watchlist updated")

        if needs_init:
            if background:
                threading.Thread(
                    target=self._activate_watchlist_pairs_safely,
                    args=(needs_init, strategy_interval),
                    daemon=True,
                    name="paper-watchlist-update",
                ).start()
            else:
                self._activate_watchlist_pairs_safely(needs_init, strategy_interval)
        else:
            self._save_session()

        return {
            "updated": True,
            "watchlist": target,
            "added": added,
            "removed": removed,
            "status": "warming" if needs_init else "active",
        }

    def remove_pair_from_watchlist(
        self,
        pair: str,
        *,
        background: bool = True,
    ) -> dict[str, Any]:
        pair = str(pair or "").strip()
        current = self._watchlist_snapshot()
        if pair not in current:
            return {
                "removed": False,
                "pair": pair,
                "watchlist": current,
                "reason": f"{pair} is not in watchlist",
            }
        target = [item for item in current if item != pair]
        result = self.update_watchlist(target, background=background)
        result["removed_pair"] = pair
        return result

    def stop(self) -> None:
        self._stop_requested = True
        if self._ws_client:
            self._ws_client.stop()
        self._save_session()

    def run(self, pairs: str | list[str], interval: str) -> None:
        if isinstance(pairs, str):
            pairs = [pair.strip() for pair in pairs.split(",") if pair.strip()]
        pairs = self._dedupe_pairs(pairs)
        if not pairs:
            raise ValueError("At least one paper-trading pair is required.")
        
        with self._watchlist_lock:
            self._watchlist = list(pairs)
            self._subscriptions = []
        self._stop_requested = False
        self._session_started_at = datetime.now(timezone.utc).isoformat()
        self._recent_diagnostics.clear()
        self._last_closed_strategy_eval_ms.clear()
        
        # Task 9: If intrabar is enabled, interval is the strategy_interval
        strategy_interval = self.settings.strategy_interval if self.settings.paper_intrabar_enabled else interval
        self._current_interval = strategy_interval
        
        _update_live_state(
            running=True,
            execution_mode=self.execution_mode,
            live_dry_run=self.live_dry_run,
            pair=", ".join(pairs),
            interval=strategy_interval,
            strategy=self.strategy_engine.strategies[0].name if self.strategy_engine.strategies else "unknown",
            starting_equity=str(self.broker.starting_equity),
            session_started_at=self._session_started_at,
            error="",
        )
        self._publish_live_snapshot(interval=strategy_interval, last_updated="starting")
        
        set_active_loop(self)
        try:
            for pair in pairs:
                self._initialize_watchlist_pair(pair, strategy_interval)
                
                if self.settings.paper_intrabar_enabled:
                    execution_interval = self.settings.execution_interval
                    self.logger.info("[%s] Intrabar execution enabled: %s -> %s", pair, strategy_interval, execution_interval)
            self._replace_subscription_snapshot(pairs, strategy_interval)
            
            pipeline = MarketDataPipeline()
            original_handle_raw = pipeline.handle_raw
            def hooked_handle_raw(event_name: str, payload: Any):
                events = original_handle_raw(event_name, payload)
                for event in events:
                    event_type = getattr(event, "event_type", None)
                    if event_type in {"orderbook_snapshot", "orderbook_update"}:
                        orderbook = pipeline.store.orderbooks.get(event.pair)
                        if orderbook is None:
                            continue
                        self.broker.update_orderbook(
                            event.pair,
                            bids=orderbook.bids,
                            asks=orderbook.asks,
                            timestamp_ms=orderbook.timestamp_ms,
                        )
                    if event_type == "candle":
                        from app.data.candle_builder import OHLCVCandle
                        candle = OHLCVCandle.from_candle_event(event)
                        self._handle_stream_candle_snapshot(candle)
                return events
            
            pipeline.handle_raw = hooked_handle_raw

            reconnect_attempt = 0
            while not self._stop_requested:
                self._ws_client = CoinDCXFuturesWebSocketClient(self.settings, pipeline=pipeline)
                subscriptions = self._subscriptions_snapshot()
                active_pairs = self._watchlist_snapshot()
                try:
                    if reconnect_attempt == 0:
                        self.logger.info(
                            "Starting live multi-pair paper loop for %s @ %s",
                            active_pairs,
                            strategy_interval,
                        )
                    else:
                        self.logger.info(
                            "Reconnecting paper websocket for %s @ %s (attempt %d)",
                            active_pairs,
                            strategy_interval,
                            reconnect_attempt + 1,
                        )
                    self._ws_client.run(subscriptions)
                    if self._stop_requested:
                        break
                    reconnect_attempt += 1
                    delay = self._websocket_reconnect_delay_seconds(reconnect_attempt)
                    message = (
                        "CoinDCX websocket disconnected unexpectedly; "
                        f"replaying missed candles and reconnecting in {delay:g}s."
                    )
                    self.logger.warning(message)
                    _update_live_state(
                        running=True,
                        error=message,
                        last_updated=datetime.now(timezone.utc).isoformat(),
                    )
                except KeyboardInterrupt:
                    self.logger.info("Graceful shutdown requested.")
                    self._stop_requested = True
                    break
                except Exception as exc:
                    reconnect_attempt += 1
                    delay = self._websocket_reconnect_delay_seconds(reconnect_attempt)
                    message = (
                        "Paper websocket failed; "
                        f"replaying missed candles and reconnecting in {delay:g}s: {exc}"
                    )
                    self.logger.exception(message)
                    _update_live_state(
                        running=True,
                        error=message,
                        last_updated=datetime.now(timezone.utc).isoformat(),
                    )
                finally:
                    if self._ws_client is not None:
                        self._ws_client.stop()
                    self._ws_client = None
                    self._save_session()

                if self._stop_requested:
                    break
                self._replay_missed_candles_after_disconnect(
                    self._watchlist_snapshot(),
                    strategy_interval,
                )
                self._sleep_before_reconnect(delay)
            
            snapshot = self.broker.snapshot()
            self.logger.info(
                "Final Paper State: equity=%.2f, open_positions=%d", 
                snapshot.equity, snapshot.open_position_count
            )
        finally:
            set_active_loop(None)
            self._ws_client = None
            _update_live_state(running=False, error="")

    def _websocket_reconnect_delay_seconds(self, attempt: int) -> float:
        return min(30.0, float(2 ** min(max(attempt - 1, 0), 5)))

    def _sleep_before_reconnect(self, delay_seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, delay_seconds)
        while not self._stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))

    def _replay_missed_candles_after_disconnect(
        self,
        pairs: list[str],
        strategy_interval: str,
    ) -> None:
        intervals = [strategy_interval]
        if (
            self.settings.paper_intrabar_enabled
            and self.settings.execution_interval != strategy_interval
        ):
            intervals.append(self.settings.execution_interval)

        for pair in pairs:
            for interval in intervals:
                try:
                    self._replay_missed_candles(pair, interval)
                except Exception:
                    self.logger.exception(
                        "[%s] Failed to replay missed %s candles after websocket reconnect.",
                        pair,
                        interval,
                    )

    def _replay_missed_candles(self, pair: str, interval: str) -> int:
        from app.backtest.data_loader import load_historical_candle_series_between

        series = (
            self.execution_series.get(pair)
            if self.settings.paper_intrabar_enabled
            and interval == self.settings.execution_interval
            else self.series.get(pair)
        )
        if series is None:
            return 0
        latest = series.latest()
        if latest is None:
            return 0

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        from_ts = int((latest.close_time_ms + 1) / 1000)
        to_ts = int(now_ms / 1000)
        if from_ts >= to_ts:
            return 0

        interval_ms = interval_to_ms(interval)
        maxlen = max(10, min(1000, int(((to_ts - from_ts) * 1000) / interval_ms) + 5))
        replay_series = load_historical_candle_series_between(
            client=self.client,
            pair=pair,
            interval=interval,
            from_ts=from_ts,
            to_ts=to_ts,
            maxlen=maxlen,
        )
        missing = [
            replace(candle, is_closed=True)
            for candle in replay_series
            if candle.close_time_ms > latest.close_time_ms and candle.close_time_ms < now_ms
        ]
        if not missing:
            return 0

        for candle in sorted(missing, key=lambda item: item.close_time_ms):
            self._on_candle(candle)
        self.logger.info(
            "[%s] Replayed %d missed %s candle(s) after websocket reconnect.",
            pair,
            len(missing),
            interval,
        )
        return len(missing)

    def _handle_stream_candle_snapshot(self, candle: OHLCVCandle) -> None:
        for closed_candle in self._closed_candles_from_stream(candle):
            self._on_candle(closed_candle)
        if (
            self.settings.paper_intrabar_enabled
            and candle.interval == self.settings.execution_interval
        ):
            self._on_candle(candle, allow_partial_execution=True)

    def _closed_candles_from_stream(self, candle: OHLCVCandle) -> list[OHLCVCandle]:
        """Convert streaming candle snapshots into finalized candles.

        CoinDCX sends repeated updates for the still-forming candle. Some payloads do
        not carry a reliable close flag, so finality is inferred when the stream
        advances to the next candle bucket.
        """
        key = (candle.pair, candle.interval)
        previous = self._pending_stream_candles.get(key)
        if previous is None:
            self._pending_stream_candles[key] = candle
            return []

        if candle.open_time_ms < previous.open_time_ms:
            return []

        if candle.open_time_ms == previous.open_time_ms:
            self._pending_stream_candles[key] = candle
            return []

        self._pending_stream_candles[key] = candle
        return [replace(previous, is_closed=True)]

    def _warm_up(self, pair: str, interval: str, lookback: int | None = None) -> None:
        from app.backtest.data_loader import (
            load_historical_candle_series,
            REST_RESOLUTION_BY_INTERVAL,
        )
        effective_lookback = max(
            200 if lookback is None else lookback,
            self._strategy_warmup_lookback_for_pair(pair),
        )
        _update_live_state(last_updated=f"warming up {pair}...")
        self.logger.info(
            "Warming up with %d REST candles for %s %s...",
            effective_lookback,
            pair,
            interval,
        )

        if interval not in REST_RESOLUTION_BY_INTERVAL:
            raise ValueError(
                f"Interval {interval!r} not supported for REST warmup. "
                f"Choose one of: {sorted(REST_RESOLUTION_BY_INTERVAL)}"
            )

        self.series[pair] = load_historical_candle_series(
            client=self.client,
            pair=pair,
            interval=interval,
            lookback=effective_lookback,
        )
        loaded = len(self.series[pair])
        self.logger.info("[%s] Warmed up with %d candles", pair, loaded)
        self._seed_candle_history(pair, self.series[pair])
        self._publish_live_snapshot(last_updated=datetime.now(timezone.utc).isoformat())
        if loaded == 0:
            message = (
                f"Warmup returned 0 candles for {pair} {interval}. "
                "Check pair format or wait for live candles to build indicators."
            )
            self.logger.warning(message)
            _update_live_state(error=message, last_updated=datetime.now(timezone.utc).isoformat())

    def _warm_up_execution(self, pair: str, interval: str, lookback: int = 200) -> None:
        from app.backtest.data_loader import load_historical_candle_series
        self.logger.info("[%s] Warming up execution series (%s)...", pair, interval)
        self.execution_series[pair] = load_historical_candle_series(
            client=self.client,
            pair=pair,
            interval=interval,
            lookback=lookback,
        )
        loaded = len(self.execution_series[pair])
        self._seed_candle_history(pair, self.execution_series[pair])
        self._publish_live_snapshot(last_updated=datetime.now(timezone.utc).isoformat())
        if loaded == 0:
            message = (
                f"Execution warmup returned 0 candles for {pair} {interval}. "
                "Check pair format or wait for live candles."
            )
            self.logger.warning(message)
            _update_live_state(error=message, last_updated=datetime.now(timezone.utc).isoformat())

    def _on_candle(
        self,
        candle: OHLCVCandle,
        *,
        allow_partial_execution: bool = False,
    ) -> None:
        if self._stop_requested:
            if self._ws_client:
                self._ws_client.stop()
            return

        self.broker.update_mark_prices({candle.pair: candle.close})

        is_live_execution_update = (
            allow_partial_execution
            and self.settings.paper_intrabar_enabled
            and candle.interval == self.settings.execution_interval
        )
        if not candle.is_closed and (not is_live_execution_update or self.settings.enter_on_execution_close):
            if candle.pair in self.broker.positions:
                self._publish_live_snapshot(last_updated=datetime.now(timezone.utc).isoformat())
            return

        pair = candle.pair
        if not self._pair_active_for_processing(pair):
            return
        if pair not in self.series:
            return

        # Determine which stream this belongs to
        is_strategy = (candle.interval == self._current_interval or self._current_interval == "unknown")
        is_execution = (self.settings.paper_intrabar_enabled and candle.interval == self.settings.execution_interval)

        if not self.settings.paper_intrabar_enabled:
            is_strategy = True
            is_execution = True

        if is_strategy:
            self._handle_strategy_candle(candle)

        if is_execution:
            self._handle_execution_candle(
                candle,
                live_partial_update=is_live_execution_update,
            )
            
        self._publish_live_snapshot(last_updated=datetime.now(timezone.utc).isoformat())

    def _handle_strategy_candle(self, candle: OHLCVCandle) -> None:
        pair = candle.pair
        series = self.series.get(pair)
        if series is None:
            return

        prev = series.latest()
        if prev is not None:
            if (
                prev.pair == candle.pair
                and prev.interval == candle.interval
                and prev.open_time_ms == candle.open_time_ms
            ):
                series.add(candle)
                return

            guard = self.gap_guards.setdefault(pair, CandleGapGuard(candle.interval))
            gap_result = guard.check(prev, candle)
            if gap_result.has_gap:
                self.logger.warning("[%s] Gap detected! Re-fetching...", pair)
                self._warm_up(pair, candle.interval)
                return

            if candle.close_time_ms <= prev.close_time_ms:
                return

        series.add(candle)
        
        if self.settings.paper_intrabar_enabled:
            if candle.open_time_ms > self._current_parent_open_ms.get(pair, 0):
                self._current_parent_open_ms[pair] = candle.open_time_ms
                self._entries_this_parent_candle[pair] = 0

    def _handle_execution_candle(
        self,
        candle: OHLCVCandle,
        *,
        live_partial_update: bool = False,
    ) -> None:
        pair = candle.pair
        if not self.settings.paper_intrabar_enabled:
            self.candle_count += 1
            self._record_live_candle(candle)
            self._process_trading_candle(candle)
            return

        series = self.execution_series.get(pair)
        if series is None:
            return
        
        prev = series.latest()
        is_same_candle_update = False
        if prev:
            if (
                prev.pair == candle.pair
                and prev.interval == candle.interval
                and prev.open_time_ms == candle.open_time_ms
            ):
                is_same_candle_update = True
            elif candle.close_time_ms <= prev.close_time_ms:
                return

        if is_same_candle_update and prev == candle and live_partial_update:
            return
            
        series.add(candle)
        self._record_live_candle(candle)
        if not is_same_candle_update:
            self.candle_count += 1
        
        from app.data.candle_builder import floor_time_ms
        parent_open_ms = floor_time_ms(candle.open_time_ms, self.settings.strategy_interval)
        if parent_open_ms > self._current_parent_open_ms.get(pair, 0):
            self._current_parent_open_ms[pair] = parent_open_ms
            self._entries_this_parent_candle[pair] = 0

        self._process_trading_candle(
            candle,
            live_partial_update=live_partial_update,
        )

    def _process_trading_candle(
        self,
        candle: OHLCVCandle,
        *,
        live_partial_update: bool = False,
    ) -> None:
        pair = candle.pair
        pair_risk = self._risk_settings_for_pair(pair)
        main_series = self.execution_series.get(pair) if self.settings.paper_intrabar_enabled else self.series.get(pair)
        if main_series is None: return
        
        if self.settings.paper_intrabar_enabled:
            htf_series = self.series[pair]
            if (
                self.settings.use_partial_parent_candle
                and self._strategy_name_for_pair(pair) != "fib_ma_pullback"
            ):
                htf_series = self._build_provisional_htf_series(pair)
            
            indicators = latest_indicator_snapshot(htf_series)
            context_interval = self.settings.strategy_interval
        else:
            indicators = latest_indicator_snapshot(self.series[pair])
            htf_series = self.series[pair]
            context_interval = candle.interval
        
        atr_val = indicators.atr

        broker_reports = self._process_candle_for_execution_mode(candle)
        self._handle_broker_reports(broker_reports, candle)

        if self.broker.starting_equity is not None and self.broker.starting_equity > 0:
            snapshot_cb = self.broker.snapshot(self._mark_prices_for_open_positions(candle))
            # Daily loss calculated from start-of-day tradable base
            drawdown = max(Decimal("0"), snapshot_cb.daily_tradable_base_start - snapshot_cb.tradable_equity)
            loss_limit = snapshot_cb.initial_equity * (
                pair_risk.max_daily_loss_pct / Decimal("100")
            )
            if drawdown >= loss_limit and not snapshot_cb.protected_profit_override_enabled:
                self.logger.warning(
                    "[circuit_breaker] Daily loss limit hit on tradable base: drawdown=%.2f limit=%.2f — "
                    "force-closing all positions and halting.",
                    drawdown,
                    loss_limit,
                )
                for report in self._force_close_all_positions(
                    candle,
                    reason="daily_loss_circuit_breaker",
                ):
                    if report.accepted and report.fill is not None:
                        self._observe_closed_fill(report.fill, candle, position=report.position)
                        trade_dict = self._map_fill_to_trade_dict(
                            fill=report.fill,
                            candle=candle,
                            position=report.position,
                        )
                        self.summary_logger.on_trade_closed(trade_dict)
                        self._closed_count += 1
                self._stop_requested = True
                return   

        bb_band = self._get_latest_bb_band(pair=pair, candle=candle)
        self.broker.update_dynamic_atr_exits(
            candle,
            atr=atr_val,
            stop_multiple=pair_risk.atr_stop_multiple,
            take_profit_multiple=pair_risk.atr_take_profit_multiple,
            trailing_multiple=pair_risk.atr_trailing_multiple,
            stop_enabled=pair_risk.atr_stop_enabled,
            take_profit_enabled=pair_risk.atr_take_profit_enabled,
            trailing_enabled=pair_risk.atr_trailing_enabled,
            take_profit_mode=pair_risk.atr_take_profit_mode,
            breakeven_enabled=pair_risk.breakeven_enabled,
            breakeven_activation_r=pair_risk.breakeven_activation_r,
            breakeven_offset_r=pair_risk.breakeven_offset_r,
            profit_lock_enabled=pair_risk.profit_lock_enabled,
            profit_lock_activation_r=pair_risk.profit_lock_activation_r,
            profit_lock_r=pair_risk.profit_lock_r,
            profit_giveback_guard_enabled=pair_risk.profit_giveback_guard_enabled,
            profit_giveback_activation_r=pair_risk.profit_giveback_activation_r,
            profit_giveback_lock_fraction=pair_risk.profit_giveback_lock_fraction,
            profit_giveback_min_lock_r=pair_risk.profit_giveback_min_lock_r,
            profit_giveback_tighten_after_r=pair_risk.profit_giveback_tighten_after_r,
            profit_giveback_tighten_fraction=pair_risk.profit_giveback_tighten_fraction,
            atr_trail_after_r_enabled=pair_risk.atr_trail_after_r_enabled,
            atr_trail_activation_r=pair_risk.atr_trail_activation_r,
            bb_band=bb_band,
            bb_trail_enabled=pair_risk.bb_trail_enabled,
            bb_trail_buffer_multiplier=pair_risk.bb_trail_buffer_multiplier,
            bb_trail_activation_r=pair_risk.bb_trail_activation_r,
            bb_trail_stage2_r=pair_risk.bb_trail_stage2_r,
            bb_trail_stage3_r=pair_risk.bb_trail_stage3_r,
            bb_trail_force_close_r=pair_risk.bb_trail_force_close_r,
            bb_trail_partial_close_at_tp=pair_risk.bb_trail_partial_close_at_tp,
            bb_trail_partial_close_pct=pair_risk.bb_trail_partial_close_pct,
            bb_trail_observe_only=pair_risk.bb_trail_observe_only,
        )

        pair_allows_new_entries = self._watchlist_contains(pair)
        if not pair_allows_new_entries and pair not in self.broker.positions:
            return

        if not self._should_evaluate_entries_on_candle(pair, candle):
            self._finalize_processed_candle(candle)
            return

        context = StrategyContext(
            pair=pair,
            interval=context_interval,
            candles=htf_series,
            indicators=indicators,
            features=self._strategy_features(pair),
        )
        signals = self._strategy_engine_for_pair(pair).evaluate(context)
        
        # Task 9: Enhanced Audit Collection
        # We find the best candidate signal (even if it's a HOLD) to represent this candle in the audit log
        audit_signal = None
        for s in signals:
            if s.action != SignalAction.HOLD:
                audit_signal = s
                break
        if audit_signal is None and signals:
            # Prefer signals with some directional bias or rejection reason
            for s in signals:
                if s.metadata.get("entry_type") or s.metadata.get("breakout_rejection"):
                    audit_signal = s
                    break
            if audit_signal is None:
                audit_signal = signals[0]

        for s in signals:
            if s.action != SignalAction.HOLD:
                self.logger.info("[%s] Signal: %s | confidence: %s | reason: %s", pair, s.action.value, s.confidence, s.reason)
            elif "No candles available" not in s.reason and "Need at least" not in s.reason and "Waiting for indicators" not in s.reason:
                # Log holds with high scores or specific reasons
                score = s.metadata.get("final_score")
                if score and abs(Decimal(str(score))) > 0.3:
                    self.logger.debug("[%s] Signal HOLD | score: %s | reason: %s", pair, score, s.reason)

        for signal in signals:
            if signal.action == SignalAction.HOLD:
                continue

            if is_entry_signal(signal) and not pair_allows_new_entries:
                continue

            if (
                live_partial_update
                and is_entry_signal(signal)
                and self.settings.enter_on_execution_close
            ):
                # Check if it's a fast entry type that bypasses the execution close wait
                is_fast_entry = signal.metadata.get("entry_type") in {
                    "balanced_breakout",
                    "pullback_continuation",
                    "intrabar_reversal_breakout",
                    "fib_ma_intrabar_pullback",
                }
                if not is_fast_entry:
                    continue

            if is_entry_signal(signal) and not _bool_metadata(
                signal.metadata.get("entry_allowed"),
                True,
            ):
                signal.metadata["safety_rejection"] = (
                    "entry_policy_blocked: "
                    f"{signal.metadata.get('atr_policy_reason', 'no policy reason')}"
                )
                continue

            if is_entry_signal(signal):
                signal = self._with_pair_runtime_metadata(signal, pair_risk)
                signal = self._with_entry_candle_metadata(signal, candle)
                signal = self._with_market_microstructure_metadata(signal)

            safety_rejection = self._paper_entry_safety_rejection(signal, candle)
            if safety_rejection is not None:
                if audit_signal and audit_signal.metadata.get("entry_type") == signal.metadata.get("entry_type"):
                    audit_signal.metadata["safety_rejection"] = safety_rejection
                continue

            if is_entry_signal(signal):
                signal = self._apply_paper_loss_throttle(signal)
                signal = self._apply_pair_recent_loss_throttle(signal)
            
            if self.settings.paper_intrabar_enabled:
                if self._entries_this_parent_candle.get(pair, 0) >= self._max_entries_per_parent_candle_for_pair(pair):
                    continue

            snapshot = self.broker.snapshot(self._mark_prices_for_open_positions(candle))
            open_positions_tuple = tuple(
                OpenPosition(
                    pair=p.pair,
                    direction=p.direction,
                    quantity=p.quantity,
                    entry_price=p.entry_price,
                    leverage=p.leverage,
                    stop_loss=p.stop_loss,
                    quote_to_margin_rate=p.quote_to_margin_rate,
                    unit_contract_value=p.unit_contract_value,
                )
                for p in self.broker.open_positions()
            )
            decision = RiskManager(pair_risk).evaluate_signal(
                signal,
                account_equity=snapshot.tradable_equity,
                available_equity=snapshot.tradable_equity,
                risk_base_mode="tradable_equity",
                open_positions=open_positions_tuple,
                daily_realized_pnl=-snapshot.daily_loss_from_tradable_base,
                daily_loss_limit_equity=snapshot.tradable_base,
                trading_mode="live" if self._live_execution_enabled else self.settings.trading_mode,
                live_trading_enabled=True if self._live_execution_enabled else self.settings.live_trading_allowed,
                requested_leverage=self._paper_leverage_for_pair(pair),
                quote_to_margin_rate=self.settings.quote_to_margin_rate,
                unit_contract_value=Decimal("1"),
                protected_profit_override_enabled=snapshot.protected_profit_override_enabled,
            )
            
            if audit_signal and audit_signal.timestamp_ms == signal.timestamp_ms:
                 audit_signal.metadata["risk_decision"] = decision

            if decision.approved:
                execution_decision = decision
                if self._live_execution_enabled:
                    live_report = self._execute_live_decision(decision)
                    if not live_report.accepted:
                        if audit_signal and audit_signal.timestamp_ms == signal.timestamp_ms:
                            audit_signal.metadata["risk_decision"] = replace(
                                decision,
                                approved=False,
                                reason=live_report.reason,
                            )
                        continue
                    execution_decision = self._with_live_execution_metadata(decision, live_report)
                    signal = execution_decision.signal
                report = self.broker.execute_decision(
                    execution_decision, market_price=candle.close, timestamp_ms=candle.close_time_ms
                )
                from app.broker.models import PaperOrderSide
                if self._live_execution_enabled and not getattr(report, "accepted", False):
                    message = f"Live execution accepted but paper shadow rejected: {report.reason}"
                    self.logger.error("[live_execution] %s", message)
                    _update_live_state(
                        error=message,
                        last_updated=datetime.now(timezone.utc).isoformat(),
                    )
                if getattr(report, 'accepted', False) and report.fill is not None:
                    if is_entry_signal(signal):
                        if report.fill.side == PaperOrderSide.BUY:
                            self._position_entry_candle[candle.pair] = self.candle_count
                            self._position_entry_prices[candle.pair] = candle.close
                        elif report.fill.side == PaperOrderSide.SELL:
                            self._position_entry_candle[candle.pair] = self.candle_count
                            self._position_entry_prices[candle.pair] = candle.close
                        
                        entry_type = signal.metadata.get("entry_type", "confirmed_trend")
                        self._entry_type_counts[entry_type] = self._entry_type_counts.get(entry_type, 0) + 1
                        
                        if self.settings.paper_intrabar_enabled:
                            self._entries_this_parent_candle[pair] = self._entries_this_parent_candle.get(pair, 0) + 1
                    elif is_exit_signal(signal) and _is_position_close_fill(report.fill):
                        self._observe_closed_fill(report.fill, candle, position=report.position)
                        trade_dict = self._map_fill_to_trade_dict(
                            fill=report.fill,
                            candle=candle,
                            position=report.position,
                        )
                        self.summary_logger.on_trade_closed(trade_dict)
                        self._closed_count += 1
                break

        # Task 9: Finalize Audit Log for this execution candle
        if audit_signal:
            self._write_audit_row(candle, audit_signal)

        self._finalize_processed_candle(candle)

    def _finalize_processed_candle(self, candle: OHLCVCandle) -> None:
        snapshot = self.broker.snapshot(self._mark_prices_for_open_positions(candle))
        
        current_eq_dict = {"t": candle.close_time_ms, "equity": str(snapshot.equity)}
        if not self.equity_history or self.equity_history[-1]["equity"] != current_eq_dict["equity"]:
            self.equity_history.append(current_eq_dict)
            if len(self.equity_history) > 1000:
                self.equity_history.pop(0)

        self.summary_logger.on_candle(
            self.candle_count, 
            snapshot.equity, 
            snapshot.open_position_count,
            self.broker.realized_pnl
        )
        self.summary_logger.check_drawdown(snapshot.equity)
        
        display_interval = f"{self.settings.strategy_interval}/{self.settings.execution_interval}" if self.settings.paper_intrabar_enabled else self._current_interval

        self._publish_live_snapshot(
            interval=display_interval,
            last_updated=datetime.now(timezone.utc).isoformat(),
        )

        self._save_session()

    def _handle_broker_reports(
        self,
        reports: list[PaperExecutionReport],
        candle: OHLCVCandle,
    ) -> None:
        for report in reports:
            if not report.accepted or report.fill is None:
                continue
            if not _is_position_close_fill(report.fill):
                continue
            self._observe_closed_fill(report.fill, candle, position=report.position)
            trade_dict = self._map_fill_to_trade_dict(
                fill=report.fill,
                candle=candle,
                position=report.position,
            )
            self.summary_logger.on_trade_closed(trade_dict)
            self._closed_count += 1

    def _with_entry_candle_metadata(
        self,
        signal: Any,
        candle: OHLCVCandle,
    ) -> Any:
        metadata = {
            **signal.metadata,
            "entry_execution_interval": candle.interval,
            "entry_execution_candle_open_time_ms": candle.open_time_ms,
            "entry_execution_candle_high": candle.high,
            "entry_execution_candle_low": candle.low,
            "entry_execution_candle_close": candle.close,
            "entry_execution_volume": candle.volume,
            "entry_execution_quote_volume": candle.quote_volume,
            "entry_execution_quote_volume_margin": (
                candle.quote_volume * self.settings.quote_to_margin_rate
            ),
        }
        return replace(signal, metadata=metadata)

    def _with_market_microstructure_metadata(self, signal: Any) -> Any:
        summary = self.broker.orderbook_summary(signal.pair)
        if not summary:
            return signal

        quote_to_margin_rate = self.settings.quote_to_margin_rate
        direction = signal.direction
        side_depth_quote = (
            summary.get("ask_depth_quote")
            if direction == SignalDirection.LONG
            else summary.get("bid_depth_quote")
        )
        side_depth_margin = (
            side_depth_quote * quote_to_margin_rate
            if isinstance(side_depth_quote, Decimal)
            else None
        )
        metadata = {
            **signal.metadata,
            "entry_orderbook_best_bid": summary.get("best_bid"),
            "entry_orderbook_best_ask": summary.get("best_ask"),
            "entry_orderbook_spread_pct": summary.get("spread_pct"),
            "entry_orderbook_bid_depth_quote": summary.get("bid_depth_quote"),
            "entry_orderbook_ask_depth_quote": summary.get("ask_depth_quote"),
            "entry_orderbook_side_depth_quote": side_depth_quote,
            "entry_orderbook_side_depth_margin": side_depth_margin,
            "entry_orderbook_timestamp_ms": summary.get("timestamp_ms"),
        }
        return replace(signal, metadata=metadata)

    def _paper_entry_safety_rejection(
        self,
        signal: Any,
        candle: OHLCVCandle,
    ) -> str | None:
        if not is_entry_signal(signal):
            return None
        if candle.close_time_ms < self._global_loss_cooldown_until_ms:
            if _paper_reentry_override(signal, strict=True):
                return None
            return "paper_loss_streak_cooldown"
        pair_cooldown = self._pair_cooldown_until_ms.get(signal.pair, 0)
        if candle.close_time_ms < pair_cooldown:
            if _paper_reentry_override(signal, strict=True):
                return None
            return "paper_pair_stop_loss_cooldown"
        if signal.direction is not None:
            key = (signal.pair, signal.direction.value)
            reversal_cooldown = self._same_direction_reversal_cooldown_until_ms.get(key, 0)
            if candle.close_time_ms < reversal_cooldown:
                return "paper_reversal_same_direction_cooldown"
            direction_cooldown = self._same_direction_cooldown_until_ms.get(key, 0)
            if candle.close_time_ms < direction_cooldown:
                if _paper_reentry_override(signal, strict=False):
                    return None
                return "paper_same_direction_cooldown"
            post_profit_state = self._post_profit_reentry_state.get(signal.pair)
            if (
                self.settings.risk.post_profit_reentry_guard_enabled
                and post_profit_state
                and post_profit_state.get("direction") == signal.direction.value
            ):
                cooldown_until_ms = int(post_profit_state.get("cooldown_until_ms") or 0)
                if candle.close_time_ms < cooldown_until_ms:
                    return "paper_post_profit_reentry_cooldown"
                if not _bool_metadata(post_profit_state.get("pullback_seen"), False):
                    exit_price = _decimal_metadata(
                        post_profit_state.get("exit_price"),
                        Decimal("0"),
                    )
                    strategy_series = self.series.get(signal.pair)
                    atr = (
                        latest_indicator_snapshot(strategy_series).atr
                        if strategy_series is not None
                        else None
                    )
                    if exit_price > 0 and post_profit_pullback_seen(
                        direction=signal.direction,
                        exit_price=exit_price,
                        candle_high=candle.high,
                        candle_low=candle.low,
                        atr=atr,
                        pullback_atr=self.settings.risk.post_profit_reentry_pullback_atr,
                        pullback_pct=self.settings.risk.post_profit_reentry_pullback_pct,
                    ):
                        self._post_profit_reentry_state[signal.pair] = {
                            **post_profit_state,
                            "pullback_seen": True,
                        }
                    else:
                        return "paper_post_profit_pullback_required"
        return None

    def _apply_paper_loss_throttle(self, signal: Any) -> Any:
        if self._consecutive_losing_trades <= 0:
            return signal

        existing = _decimal_metadata(signal.metadata.get("risk_multiplier"), Decimal("1"))
        throttle = Decimal("0.50")
        metadata = {
            **signal.metadata,
            "risk_multiplier": min(existing, throttle),
            "risk_multiplier_applies": True,
            "paper_loss_throttle_active": True,
            "paper_consecutive_losing_trades": self._consecutive_losing_trades,
        }
        return replace(signal, metadata=metadata)

    def _apply_pair_recent_loss_throttle(self, signal: Any) -> Any:
        if not is_entry_signal(signal):
            return signal
        recent = self._pair_recent_net_pnls.get(signal.pair, [])
        profile = pair_recent_risk_profile(recent, self._risk_settings_for_pair(signal.pair))
        if profile.multiplier >= 1:
            if not profile.metadata:
                return signal
            return replace(
                signal,
                metadata={**signal.metadata, **profile.metadata},
            )

        existing = _decimal_metadata(signal.metadata.get("risk_multiplier"), Decimal("1"))
        metadata = {
            **signal.metadata,
            **profile.metadata,
            "risk_multiplier": min(existing, profile.multiplier),
            "risk_multiplier_applies": True,
        }
        return replace(signal, metadata=metadata)

    def _observe_closed_fill(
        self,
        fill: PaperFill,
        candle: OHLCVCandle,
        *,
        position: PaperPosition | None = None,
    ) -> None:
        metadata = fill.metadata if isinstance(fill.metadata, dict) else {}
        position_metadata = (
            position.metadata
            if position is not None and isinstance(position.metadata, dict)
            else {}
        )
        exit_reason = _paper_exit_reason(fill).lower()
        entry_fee_source = (
            metadata.get("entry_fee_allocated")
            or position_metadata.get("entry_fee_allocated")
            or position_metadata.get("entry_fees_total")
            or position_metadata.get("entry_fee")
        )
        entry_fee = _decimal_metadata(
            entry_fee_source,
            Decimal("0"),
        )
        net_pnl = fill.realized_pnl - fill.fee - entry_fee
        self._record_pair_net_pnl(fill.pair, net_pnl)
        if _bool_metadata(metadata.get("partial_close"), False):
            return
        interval_ms = self._paper_safety_interval_ms()

        if net_pnl < 0:
            self._consecutive_losing_trades += 1
            if _paper_stop_style_exit(exit_reason):
                until_ms = candle.close_time_ms + (10 * interval_ms)
                self._pair_cooldown_until_ms[fill.pair] = max(
                    self._pair_cooldown_until_ms.get(fill.pair, 0),
                    until_ms,
                )
                self.logger.info(
                    "[%s] Paper safety: stop-loss cooldown active for 10 execution candles.",
                    fill.pair,
                )
            if self._consecutive_losing_trades >= 2:
                self._global_loss_cooldown_until_ms = max(
                    self._global_loss_cooldown_until_ms,
                    candle.close_time_ms + (20 * interval_ms),
                )
                self.logger.info(
                    "Paper safety: global loss-streak cooldown active after %d losses.",
                    self._consecutive_losing_trades,
                )
            return

        if net_pnl > 0:
            self._consecutive_losing_trades = 0
            direction = _closed_position_direction(fill)
            if direction is None:
                return
            key = (fill.pair, direction.value)
            if self.settings.risk.post_profit_reentry_guard_enabled:
                cooldown_candles = max(
                    self.settings.risk.post_profit_reentry_cooldown_candles,
                    0,
                )
                cooldown_until_ms = (
                    candle.close_time_ms + (cooldown_candles * interval_ms)
                )
                self._same_direction_cooldown_until_ms[key] = max(
                    self._same_direction_cooldown_until_ms.get(key, 0),
                    cooldown_until_ms,
                )
                if _paper_stop_style_exit(exit_reason):
                    self._same_direction_reversal_cooldown_until_ms[key] = max(
                        self._same_direction_reversal_cooldown_until_ms.get(key, 0),
                        cooldown_until_ms,
                    )
                self._post_profit_reentry_state[fill.pair] = {
                    "direction": direction.value,
                    "exit_price": str(fill.price),
                    "closed_at_ms": str(candle.close_time_ms),
                    "cooldown_until_ms": str(cooldown_until_ms),
                    "pullback_seen": False,
                    "net_pnl": str(net_pnl),
                }
                self.logger.info(
                    "[%s] Paper safety: post-profit %s guard active for %d execution candles.",
                    fill.pair,
                    direction.value,
                    cooldown_candles,
                )
            elif _paper_stop_style_exit(exit_reason):
                self._same_direction_reversal_cooldown_until_ms[key] = max(
                    self._same_direction_reversal_cooldown_until_ms.get(key, 0),
                    candle.close_time_ms + (12 * interval_ms),
                )
                self.logger.info(
                    "[%s] Paper safety: same-direction cooldown after reversal stop.",
                    fill.pair,
                )
            elif _paper_take_profit_style_exit(exit_reason):
                self._same_direction_cooldown_until_ms[key] = max(
                    self._same_direction_cooldown_until_ms.get(key, 0),
                    candle.close_time_ms + (12 * interval_ms),
                )
                self.logger.info(
                    "[%s] Paper safety: same-direction cooldown after take-profit.",
                    fill.pair,
                )

    def _record_pair_net_pnl(self, pair: str, net_pnl: Decimal) -> None:
        lookback = max(self.settings.risk.pair_loss_lookback, 1)
        values = self._pair_recent_net_pnls.setdefault(pair, [])
        values.append(net_pnl)
        if len(values) > lookback:
            del values[:-lookback]

    def _paper_safety_interval_ms(self) -> int:
        interval = (
            self.settings.execution_interval
            if self.settings.paper_intrabar_enabled
            else self._current_interval
        )
        try:
            return interval_to_ms(interval)
        except Exception:
            return 60_000

    def _get_latest_bb_band(
        self,
        pair: str,
        candle: OHLCVCandle,
    ) -> BollingerBandPoint | None:
        """
        Return the most recently computed Bollinger band for this pair.

        The trail uses live candle history instead of stale entry metadata so
        the middle-band reference can follow the trade while it is open.
        """

        if self.settings.paper_intrabar_enabled:
            series = (
                self._build_provisional_htf_series(pair)
                if self.settings.use_partial_parent_candle
                else self.series.get(pair)
            )
        else:
            series = self.series.get(pair)
        if series is None or len(series) < 20:
            return None
        bands = bollinger_bands(series.closes(), period=20)
        return bands[-1] if bands else None

    def _parent_completion_ratio(self, pair: str) -> Decimal:
        if not (self.settings.paper_intrabar_enabled and self.settings.use_partial_parent_candle):
            return Decimal("1")
        exec_series = self.execution_series.get(pair)
        if exec_series is None:
            return Decimal("1")
        latest_exec = exec_series.latest()
        if latest_exec is None:
            return Decimal("1")

        from app.data.candle_builder import floor_time_ms

        try:
            parent_ms = interval_to_ms(self.settings.strategy_interval)
        except Exception:
            return Decimal("1")
        if parent_ms <= 0:
            return Decimal("1")

        parent_open_ms = floor_time_ms(latest_exec.open_time_ms, self.settings.strategy_interval)
        elapsed_ms = latest_exec.close_time_ms - parent_open_ms + 1
        elapsed_ms = max(0, min(elapsed_ms, parent_ms))
        if elapsed_ms <= 0:
            return Decimal("1")
        return Decimal(elapsed_ms) / Decimal(parent_ms)

    def _strategy_features(self, pair: str) -> dict[str, Any]:
        execution_candles: list[OHLCVCandle] = []
        exec_series = self.execution_series.get(pair)
        if exec_series is not None:
            execution_candles = list(exec_series)[-30:]

        # Get the high/low of the last fully closed parent candle for breakout checks
        previous_parent_high = Decimal("0")
        previous_parent_low = Decimal("0")
        htf_series = self.series.get(pair)
        if htf_series is not None and len(htf_series) > 0:
            last_closed = htf_series.latest()
            if last_closed is not None:
                previous_parent_high = last_closed.high
                previous_parent_low = last_closed.low

        pair_risk = self._risk_settings_for_pair(pair)
        strategy_name = self._strategy_name_for_pair(pair)
        profile_config, pair_profile = apply_pair_profile_to_config(
            pair,
            {
                "risk_per_trade_pct": pair_risk.max_risk_per_trade_pct,
                "trade_quality_mode": "strict",
                "controlled_shorts_enabled": False,
                "a_setup_score_threshold": Decimal("0.50"),
                "a_setup_agreement_threshold": A_SETUP_AGREEMENT_THRESHOLD,
                "b_setup_score_threshold": Decimal("0.40"),
                "b_setup_agreement_threshold": B_SETUP_AGREEMENT_THRESHOLD,
                "b_setup_risk_multiplier": Decimal("0.50"),
                "minimum_visual_score": Decimal("0"),
                "long_entry_threshold": DEFAULT_DIRECTIONAL_ENTRY_THRESHOLD,
                "short_entry_threshold": DEFAULT_DIRECTIONAL_ENTRY_THRESHOLD,
                "short_agreement_threshold": B_SETUP_AGREEMENT_THRESHOLD,
                "trailing_stop_enabled": pair_risk.trailing_stop_enabled,
                "trailing_stop_activation_pct": pair_risk.trailing_stop_activation_pct,
                "trailing_stop_distance_pct": pair_risk.trailing_stop_distance_pct,
                "atr_dynamic_exits_enabled": (
                    pair_risk.atr_stop_enabled
                    or pair_risk.atr_take_profit_enabled
                    or pair_risk.atr_trailing_enabled
                ),
                "atr_stop_enabled": pair_risk.atr_stop_enabled,
                "atr_take_profit_enabled": pair_risk.atr_take_profit_enabled,
                "atr_trailing_enabled": pair_risk.atr_trailing_enabled,
                "profit_lock_enabled": pair_risk.profit_lock_enabled,
                "profit_giveback_guard_enabled": pair_risk.profit_giveback_guard_enabled,
                "profit_giveback_activation_r": pair_risk.profit_giveback_activation_r,
                "profit_giveback_lock_fraction": pair_risk.profit_giveback_lock_fraction,
                "profit_giveback_min_lock_r": pair_risk.profit_giveback_min_lock_r,
                "profit_giveback_tighten_after_r": pair_risk.profit_giveback_tighten_after_r,
                "profit_giveback_tighten_fraction": pair_risk.profit_giveback_tighten_fraction,
                "bb_trail_enabled": pair_risk.bb_trail_enabled,
                "bb_trail_buffer_multiplier": pair_risk.bb_trail_buffer_multiplier,
                "bb_trail_activation_r": pair_risk.bb_trail_activation_r,
                "bb_trail_stage2_r": pair_risk.bb_trail_stage2_r,
                "bb_trail_stage3_r": pair_risk.bb_trail_stage3_r,
                "bb_trail_force_close_r": pair_risk.bb_trail_force_close_r,
                "bb_trail_partial_close_at_tp": pair_risk.bb_trail_partial_close_at_tp,
                "bb_trail_partial_close_pct": pair_risk.bb_trail_partial_close_pct,
                "bb_trail_observe_only": pair_risk.bb_trail_observe_only,
                "atr_entry_filter_enabled": True,
                "atr_policy_mode": "router",
                "intrabar_reversal_breakout_enabled": (
                    self.settings.paper_intrabar_enabled
                    and strategy_name == "hybrid_meta_v2"
                ),
                "previous_parent_high": previous_parent_high,
                "previous_parent_low": previous_parent_low,
                "parent_completion_ratio": self._parent_completion_ratio(pair),
                "reversal_breakout_min_execution_candles": 2,
                "reversal_breakout_volume_ratio": Decimal("2.0"),
                "reversal_breakout_body_ratio": Decimal("0.65"),
                "reversal_breakout_close_position_ratio": Decimal("0.70"),
                "reversal_breakout_risk_multiplier": Decimal("0.50"),
                "reversal_breakout_max_extension_atr": Decimal("2.2"),
                "reversal_breakout_ignition_volume_ratio": Decimal("3.0"),
                "reversal_breakout_ignition_body_ratio": Decimal("0.70"),
                "reversal_breakout_ignition_close_position_ratio": Decimal("0.75"),
                "reversal_breakout_ignition_max_extension_atr": Decimal("5.0"),
                "reversal_breakout_ignition_risk_multiplier": Decimal("0.25"),
                "reversal_breakout_breakeven_activation_r": Decimal("0.70"),
                "reversal_breakout_profit_lock_activation_r": Decimal("1.20"),
                "reversal_breakout_profit_lock_r": Decimal("0.35"),
                "reversal_breakout_time_stop_candles": 8,
                # New entry timing fields
                "balanced_breakout_enabled": pair_risk.balanced_breakout_enabled,
                "balanced_breakout_volume_ratio_min": pair_risk.balanced_breakout_volume_ratio_min,
                "balanced_breakout_body_ratio_min": pair_risk.balanced_breakout_body_ratio_min,
                "balanced_breakout_close_position_min": pair_risk.balanced_breakout_close_position_min,
                "balanced_breakout_max_extension_atr": pair_risk.balanced_breakout_max_extension_atr,
                "balanced_breakout_max_age_candles": pair_risk.balanced_breakout_max_age_candles,
                "balanced_breakout_risk_multiplier": pair_risk.balanced_breakout_risk_multiplier,
                "false_breakout_filter_enabled": pair_risk.false_breakout_filter_enabled,
                "false_breakout_max_wick_ratio": pair_risk.false_breakout_max_wick_ratio,
                "false_breakout_require_close_outside_parent": pair_risk.false_breakout_require_close_outside_parent,
                "late_chase_block_enabled": pair_risk.late_chase_block_enabled,
                "late_chase_max_consecutive_impulse_candles": pair_risk.late_chase_max_consecutive_impulse_candles,
                "late_chase_volume_fade_ratio": pair_risk.late_chase_volume_fade_ratio,
                "late_chase_max_extension_atr": pair_risk.late_chase_max_extension_atr,
                "pullback_entry_enabled": pair_risk.pullback_entry_enabled,
                "pullback_max_age_candles": pair_risk.pullback_max_age_candles,
                "pullback_max_distance_from_ema_atr": pair_risk.pullback_max_distance_from_ema_atr,
                "pullback_resume_body_ratio_min": pair_risk.pullback_resume_body_ratio_min,
                "pullback_risk_multiplier": pair_risk.pullback_risk_multiplier,
                "signal_flip_grace_candles": pair_risk.signal_flip_grace_candles,
                "signal_flip_confirm_candles": pair_risk.signal_flip_confirm_candles,
                "time_stop_extend_if_momentum_strong": pair_risk.time_stop_extend_if_momentum_strong,
            },
        )
        features: dict[str, Any] = {
            "backtest_config": profile_config,
            "pair_profile": pair_profile.metadata(),
            "pair_overrides": self._pair_override(pair),
        }
        if execution_candles:
            features["execution_candles"] = execution_candles
            features["execution_interval"] = self.settings.execution_interval
        for position in self.broker.open_positions():
            if position.pair != pair:
                continue
            features["open_position"] = {
                "pair": position.pair,
                "direction": position.direction.value,
                "quantity": position.quantity,
                "entry_price": position.entry_price,
                "opened_at_ms": position.opened_at_ms,
                "strategy_name": position.strategy_name,
                "stop_loss": position.stop_loss,
                "take_profit": position.take_profit,
                "metadata": position.metadata,
            }
            break
        return features

    def _build_provisional_htf_series(self, pair: str) -> CandleSeries:
        from app.data.candle_builder import floor_time_ms
        exec_series = self.execution_series.get(pair)
        base_series = self.series.get(pair)
        if exec_series is None or base_series is None:
            return base_series or CandleSeries()
            
        latest_exec = exec_series.latest()
        if latest_exec is None:
            return base_series
            
        parent_open_ms = floor_time_ms(latest_exec.open_time_ms, self.settings.strategy_interval)
        intrabar_candles = [c for c in exec_series if c.open_time_ms >= parent_open_ms]
        if not intrabar_candles:
            return base_series
            
        from app.data.candle_builder import interval_to_ms
        provisional = OHLCVCandle(
            pair=latest_exec.pair,
            interval=self.settings.strategy_interval,
            open_time_ms=parent_open_ms,
            close_time_ms=parent_open_ms + interval_to_ms(self.settings.strategy_interval) - 1,
            open=intrabar_candles[0].open,
            high=max(c.high for c in intrabar_candles),
            low=min(c.low for c in intrabar_candles),
            close=intrabar_candles[-1].close,
            volume=sum(c.volume for c in intrabar_candles),
            quote_volume=sum(c.quote_volume for c in intrabar_candles),
            trade_count=sum(c.trade_count for c in intrabar_candles)
        )
        
        new_series = CandleSeries(list(base_series), maxlen=base_series.maxlen)
        new_series.add(provisional)
        return new_series

    def _map_fill_to_trade_dict(
        self,
        fill: Any,
        candle: OHLCVCandle,
        *,
        position: PaperPosition | None = None,
    ) -> dict[str, Any]:
        position_metadata = (
            position.metadata
            if position is not None and isinstance(position.metadata, dict)
            else {}
        )
        fill_metadata = fill.metadata if isinstance(fill.metadata, dict) else {}
        entry_price = (
            position.entry_price
            if position is not None
            else self._position_entry_prices.get(fill.pair, fill.price)
        )
        entry_candle = self._position_entry_candle.get(fill.pair, self.candle_count)
        if position is not None:
            interval_ms = self._paper_safety_interval_ms()
            hold = max(0, int((fill.timestamp_ms - position.opened_at_ms) // interval_ms))
        else:
            hold = self.candle_count - entry_candle
        gross = fill.realized_pnl
        entry_fee_source = (
            fill_metadata.get("entry_fee_allocated")
            or position_metadata.get("entry_fee_allocated")
            or position_metadata.get("entry_fees_total")
            or position_metadata.get("entry_fee")
        )
        entry_fee = _decimal_metadata(
            entry_fee_source,
            Decimal("0"),
        )
        exit_fee = fill.fee
        total_fees = entry_fee + exit_fee
        net = gross - total_fees
        quote_to_margin_rate = _decimal_metadata(
            position_metadata.get("quote_to_margin_rate")
            or fill_metadata.get("quote_to_margin_rate"),
            self.settings.quote_to_margin_rate,
        )
        unit_contract_value = _decimal_metadata(
            position_metadata.get("unit_contract_value")
            or fill_metadata.get("unit_contract_value"),
            Decimal("1"),
        )
        notional = entry_price * fill.quantity * quote_to_margin_rate * unit_contract_value
        exit_notional = fill.price * fill.quantity * quote_to_margin_rate * unit_contract_value
        
        required_margin = _decimal_metadata(
            position_metadata.get("required_margin")
            or fill_metadata.get("required_margin"),
            Decimal("0"),
        )
        leverage = _decimal_metadata(
            position_metadata.get("leverage")
            or fill_metadata.get("leverage")
            or (position.leverage if position is not None else None),
            Decimal("0"),
        )
        if leverage <= 0 and notional > 0 and required_margin > 0:
            leverage = notional / required_margin
        if leverage <= 0:
            leverage = Decimal("1")
        if required_margin <= 0 and notional > 0:
            required_margin = notional / leverage if leverage > 0 else notional
        margin_used = required_margin if required_margin > 0 else Decimal("0")
        
        net_pct = (net / notional * Decimal("100")) if notional > 0 else Decimal("0")
        gross_roe_pct = (gross / margin_used * Decimal("100")) if margin_used > 0 else Decimal("0")
        net_roe_pct = (net / margin_used * Decimal("100")) if margin_used > 0 else Decimal("0")
        
        account_equity_at_entry = _decimal_metadata(
            position_metadata.get("account_equity_at_entry"),
            Decimal("0"),
        )
        account_impact_pct = (net / account_equity_at_entry * Decimal("100")) if account_equity_at_entry > 0 else Decimal("0")

        legacy_currency_math = (
            position is not None
            and _decimal_metadata(position_metadata.get("quote_to_margin_rate"), quote_to_margin_rate)
            != self.settings.quote_to_margin_rate
        ) or (
            "quote_to_margin_rate" not in fill_metadata
            and "quote_to_margin_rate" not in position_metadata
            and self.settings.quote_to_margin_rate != Decimal("1")
        )
        
        # direction: if fill.side is SELL, position was LONG (we sold to close); vice versa
        from app.broker.models import PaperOrderSide
        direction = "long" if fill.side == PaperOrderSide.SELL else "short"
        
        if not _bool_metadata(fill_metadata.get("partial_close"), False):
            self._position_entry_candle.pop(fill.pair, None)
            self._position_entry_prices.pop(fill.pair, None)

        snapshot = self.broker.snapshot({fill.pair: fill.price})
        amount_locked = Decimal("0")
        if net > 0 and self.broker.profit_lock_enabled:
            amount_locked = net * (self.broker.auto_lock_profit_pct / Decimal("100"))

        return {
            "fill_id": str(getattr(fill, "fill_id", "") or ""),
            "order_id": str(getattr(fill, "order_id", "") or ""),
            "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(fill.timestamp_ms / 1000)),
            "pair": fill.pair,
            "interval": candle.interval,
            "strategy": position.strategy_name if position is not None else self._strategy_name_for_pair(fill.pair),
            "paper_pair_strategy": str(position_metadata.get("paper_pair_strategy") or ""),
            "paper_pair_leverage": str(position_metadata.get("paper_pair_leverage") or ""),
            "direction": direction,
            "entry_price": str(entry_price),
            "exit_price": str(fill.price),
            "stop_loss": str(
                fill_metadata.get("atr_stop_loss")
                or position_metadata.get("atr_stop_loss")
                or fill_metadata.get("initial_stop_loss")
                or position_metadata.get("initial_stop_loss")
                or ""
            ),
            "take_profit": str(
                fill_metadata.get("atr_take_profit")
                or position_metadata.get("atr_take_profit")
                or fill_metadata.get("take_profit")
                or position_metadata.get("take_profit")
                or ""
            ),
            "position_size": str(fill.quantity),
            "quantity_unit": _quantity_unit(fill.pair),
            "position_notional": str(notional),
            "entry_notional": str(notional),
            "exit_notional": str(exit_notional),
            "leverage": str(leverage),
            "margin_used": str(margin_used),
            "notional_currency": self.settings.futures_margin_currency,
            "price_quote_currency": self.settings.price_quote_currency,
            "quote_to_margin_rate": str(quote_to_margin_rate),
            "unit_contract_value": str(unit_contract_value),
            "risk_percent_used": str(position_metadata.get("risk_percent_used") or fill_metadata.get("risk_percent_used") or ""),
            "risk_multiplier": str(position_metadata.get("risk_multiplier") or fill_metadata.get("risk_multiplier") or ""),
            "risk_base_mode": str(position_metadata.get("risk_base_mode") or fill_metadata.get("risk_base_mode") or ""),
            "risk_base_amount": str(position_metadata.get("risk_base_amount") or fill_metadata.get("risk_base_amount") or ""),
            "planned_risk_amount": str(position_metadata.get("planned_risk_amount") or fill_metadata.get("planned_risk_amount") or ""),
            "trailing_stop_enabled": str(_bool_metadata(position_metadata.get("trailing_stop_enabled"), False)).lower(),
            "trailing_stop_active": str(_bool_metadata(position_metadata.get("trailing_stop_active"), False)).lower(),
            "atr_dynamic_exits_enabled": str(_bool_metadata(position_metadata.get("atr_dynamic_exits_enabled"), False)).lower(),
            "bb_trail_enabled": str(_bool_metadata(position_metadata.get("bb_trail_enabled"), False)).lower(),
            "profit_lock_enabled": str(_bool_metadata(position_metadata.get("profit_lock_enabled"), False)).lower(),
            "required_margin": str(required_margin),
            "available_equity": str(snapshot.equity),
            "margin_ok": str(fill_metadata.get("margin_ok", True)),
            "account_blown": str(snapshot.equity <= 0).lower(),
            "fee_type": str(fill_metadata.get("fee_type") or ""),
            "fee_rate": str(fill_metadata.get("fee_rate") or ""),
            "fee_gst_rate": str(fill_metadata.get("fee_gst_rate") or ""),
            "effective_fee_rate": str(fill_metadata.get("effective_fee_rate") or ""),
            "entry_fee": str(entry_fee),
            "exit_fee": str(exit_fee),
            "total_fees": str(total_fees),
            "gross_pnl": str(gross),
            "fees": str(total_fees),
            "net_pnl": str(net),
            "net_pnl_pct": str(net_pct),
            "gross_roe_pct": str(gross_roe_pct),
            "net_roe_pct": str(net_roe_pct),
            "account_equity_at_entry": str(account_equity_at_entry),
            "account_impact_pct": str(account_impact_pct),
            "equity_after": str(snapshot.equity),
            "amount_locked_from_trade": str(amount_locked),
            "tradable_equity_after_trade": str(snapshot.tradable_equity),
            "locked_profit_after_trade": str(snapshot.locked_profit),
            "daily_loss_after_trade": str(snapshot.daily_loss_from_tradable_base),
            "exit_reason": _paper_exit_reason(fill),
            "hold_duration_candles": hold,
            "legacy_currency_math": str(legacy_currency_math).lower(),
        }
