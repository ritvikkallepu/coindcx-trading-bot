from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from app.broker.models import PaperExecutionReport
from app.broker.paper import PaperBroker
from app.data.candle_builder import OHLCVCandle
from app.risk.models import RiskDecision


class PaperExecutionEngine:
    def __init__(self, broker: PaperBroker) -> None:
        self.broker = broker

    def process_decision(
        self,
        decision: RiskDecision,
        *,
        market_price: Decimal | None = None,
        timestamp_ms: int | None = None,
    ) -> PaperExecutionReport:
        return self.broker.execute_decision(
            decision,
            market_price=market_price,
            timestamp_ms=timestamp_ms,
        )

    def process_decisions(
        self,
        decisions: Iterable[RiskDecision],
        *,
        market_price: Decimal | None = None,
        timestamp_ms: int | None = None,
    ) -> list[PaperExecutionReport]:
        return [
            self.process_decision(
                decision,
                market_price=market_price,
                timestamp_ms=timestamp_ms,
            )
            for decision in decisions
        ]

    def process_candle(self, candle: OHLCVCandle) -> list[PaperExecutionReport]:
        return self.broker.process_candle(candle)
