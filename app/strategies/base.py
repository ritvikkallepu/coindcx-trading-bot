from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from app.data.candle_builder import CandleSeries, OHLCVCandle
from app.data.indicators import IndicatorSnapshot


from enum import Enum
from typing import Any, Mapping

class SignalAction(str, Enum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT_LONG = "exit_long"
    EXIT_SHORT = "exit_short"
    HOLD = "hold"


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


class SignalFunnelReason(str, Enum):
    # Success
    EXECUTED = "executed"
    
    # Strategy Filters
    BELOW_ENTRY_THRESHOLD = "below_entry_threshold"
    BELOW_LONG_THRESHOLD = "below_long_threshold"
    BELOW_SHORT_THRESHOLD = "below_short_threshold"
    BELOW_A_SETUP_THRESHOLD = "below_a_setup_threshold"
    BELOW_B_SETUP_THRESHOLD = "below_b_setup_threshold"
    TRADE_QUALITY_C_SKIPPED = "trade_quality_c_skipped"
    FINAL_SCORE_TOO_WEAK = "final_score_too_weak"
    AGREEMENT_BELOW_MINIMUM = "agreement_below_minimum"
    MINIMUM_VISUAL_SCORE_FAILED = "minimum_visual_score_failed"
    VISUAL_SCREEN_BLOCKED = "visual_screen_blocked"
    SHORT_THRESHOLD_FAILED = "short_threshold_failed"
    LONG_THRESHOLD_FAILED = "long_threshold_failed"
    CONTROLLED_SHORT_BLOCKED = "controlled_short_blocked"
    OI_SHORT_RESTRICTION = "oi_short_restriction"
    ATR_POLICY_ENTRY_BLOCKED = "atr_policy_entry_blocked"
    
    # Execution / Risk Filters
    COOLDOWN_BLOCKED = "cooldown_blocked"
    EXISTING_POSITION_BLOCKED = "existing_position_blocked"
    DAILY_LOSS_GUARD_BLOCKED = "daily_loss_guard_blocked"
    EXPOSURE_MARGIN_BLOCKED = "exposure_margin_blocked"
    
    # Unmapped / Catch-all
    RISK_REJECTION_UNMAPPED = "risk_rejection_unmapped"
    STRATEGY_HOLD_UNMAPPED = "strategy_hold_unmapped"
    UNEXPLAINED_CANDIDATE = "unexplained_candidate"


@dataclass(frozen=True)
class StrategySignal:
    strategy_name: str
    pair: str
    interval: str
    action: SignalAction
    confidence: Decimal
    reason: str
    timestamp_ms: int
    direction: SignalDirection | None = None
    entry_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def funnel_reason(self) -> SignalFunnelReason | None:
        raw = self.metadata.get("funnel_reason")
        if isinstance(raw, SignalFunnelReason):
            return raw
        if isinstance(raw, str):
            try:
                return SignalFunnelReason(raw)
            except ValueError:
                return None
        return None

    @classmethod
    def hold(
        cls,
        *,
        strategy_name: str,
        pair: str,
        interval: str,
        timestamp_ms: int,
        reason: str,
        funnel_reason: SignalFunnelReason | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StrategySignal:
        merged_metadata = dict(metadata or {})
        if funnel_reason:
            merged_metadata["funnel_reason"] = (
                funnel_reason.value if isinstance(funnel_reason, Enum) else funnel_reason
            )
        return cls(
            strategy_name=strategy_name,
            pair=pair,
            interval=interval,
            action=SignalAction.HOLD,
            confidence=Decimal("0"),
            reason=reason,
            timestamp_ms=timestamp_ms,
            metadata=merged_metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            if isinstance(value, list):
                return [convert(item) for item in value]
            return value

        return convert(asdict(self))


@dataclass(frozen=True)
class StrategyContext:
    pair: str
    interval: str
    candles: CandleSeries
    indicators: IndicatorSnapshot
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def latest_candle(self) -> OHLCVCandle | None:
        return self.candles.latest()


class Strategy(ABC):
    name: str

    @abstractmethod
    def evaluate(self, context: StrategyContext) -> StrategySignal:
        raise NotImplementedError


class StrategyEngine:
    def __init__(
        self,
        strategies: list[Strategy],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        if not strategies:
            raise ValueError("StrategyEngine requires at least one strategy.")
        self.strategies = strategies
        self.logger = logger or logging.getLogger(__name__)

    def evaluate(self, context: StrategyContext) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        for strategy in self.strategies:
            try:
                signals.append(strategy.evaluate(context))
            except Exception:
                self.logger.exception("Strategy %s failed; emitting hold signal.", strategy.name)
                signals.append(
                    StrategySignal.hold(
                        strategy_name=strategy.name,
                        pair=context.pair,
                        interval=context.interval,
                        timestamp_ms=_context_timestamp(context),
                        reason="Strategy evaluation failed.",
                    )
                )
        return signals


def clamp_confidence(value: Decimal) -> Decimal:
    return min(max(value, Decimal("0")), Decimal("1"))


def _context_timestamp(context: StrategyContext) -> int:
    latest = context.latest_candle
    if latest is None:
        return 0
    return latest.close_time_ms
