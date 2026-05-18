from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

from app.data.candle_builder import CandleSeries, OHLCVCandle
from app.data.indicators import IndicatorSnapshot


class SignalAction(str, Enum):
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT_LONG = "exit_long"
    EXIT_SHORT = "exit_short"
    HOLD = "hold"


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


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

    @classmethod
    def hold(
        cls,
        *,
        strategy_name: str,
        pair: str,
        interval: str,
        timestamp_ms: int,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> StrategySignal:
        return cls(
            strategy_name=strategy_name,
            pair=pair,
            interval=interval,
            action=SignalAction.HOLD,
            confidence=Decimal("0"),
            reason=reason,
            timestamp_ms=timestamp_ms,
            metadata=metadata or {},
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
