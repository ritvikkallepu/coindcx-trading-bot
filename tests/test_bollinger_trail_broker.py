from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from app.broker.paper import PaperBroker
from app.data.candle_builder import OHLCVCandle
from app.data.indicators import BollingerBandPoint
from app.risk.models import RiskDecision
from app.strategies.base import SignalAction, SignalDirection, StrategySignal


def _candle(
    *,
    open_price: Decimal = Decimal("100"),
    high: Decimal = Decimal("100"),
    low: Decimal = Decimal("100"),
    close: Decimal = Decimal("100"),
    timestamp_ms: int = 300_000,
) -> OHLCVCandle:
    return OHLCVCandle(
        pair="B-BTC_USDT",
        interval="5m",
        open_time_ms=timestamp_ms,
        close_time_ms=timestamp_ms + 299_999,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=Decimal("100"),
    )


class BollingerTrailBrokerTests(unittest.TestCase):
    def _open_position(
        self,
        *,
        direction: SignalDirection,
        stop_loss: Decimal,
    ) -> PaperBroker:
        broker = PaperBroker(starting_equity=Decimal("10000"))
        signal = StrategySignal(
            strategy_name="test",
            pair="B-BTC_USDT",
            interval="5m",
            action=(
                SignalAction.ENTER_LONG
                if direction == SignalDirection.LONG
                else SignalAction.ENTER_SHORT
            ),
            direction=direction,
            confidence=Decimal("1"),
            reason="entry",
            timestamp_ms=0,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95") if direction == SignalDirection.LONG else Decimal("105"),
            take_profit=Decimal("110") if direction == SignalDirection.LONG else Decimal("90"),
            metadata={},
        )
        decision = RiskDecision(
            approved=True,
            reason="ok",
            signal=signal,
            position_size=Decimal("10"),
        )
        entry = broker.execute_decision(
            decision,
            market_price=Decimal("100"),
            timestamp_ms=0,
        )
        self.assertTrue(entry.accepted, entry.reason)
        position = broker.open_positions()[0]
        broker.positions[position.pair] = replace(
            position,
            stop_loss=stop_loss,
            metadata={
                **position.metadata,
                "bb_trail_enabled": True,
                "bb_trail_active": True,
                "bb_trail_stop": stop_loss,
                "stop_type": "bb_trail",
                "atr_stop_enabled": False,
                "atr_dynamic_exit_active": False,
            },
        )
        return broker

    def test_bb_stop_reason_does_not_require_atr_exits_for_long(self) -> None:
        broker = self._open_position(
            direction=SignalDirection.LONG,
            stop_loss=Decimal("101"),
        )

        reports = broker.process_candle(
            _candle(
                open_price=Decimal("102"),
                high=Decimal("103"),
                low=Decimal("100"),
                close=Decimal("100.5"),
                timestamp_ms=600_000,
            )
        )

        self.assertEqual(len(reports), 1)
        self.assertEqual(
            reports[0].reason,
            "Bollinger Band hybrid trail stop triggered.",
        )

    def test_bb_stop_reason_does_not_require_atr_exits_for_short(self) -> None:
        broker = self._open_position(
            direction=SignalDirection.SHORT,
            stop_loss=Decimal("99"),
        )

        reports = broker.process_candle(
            _candle(
                open_price=Decimal("98"),
                high=Decimal("99.5"),
                low=Decimal("97"),
                close=Decimal("98.5"),
                timestamp_ms=600_000,
            )
        )

        self.assertEqual(len(reports), 1)
        self.assertEqual(
            reports[0].reason,
            "Bollinger Band hybrid trail stop triggered.",
        )

    def test_bb_partial_tp_reduces_position_once(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("10000"))
        signal = StrategySignal(
            strategy_name="test",
            pair="B-BTC_USDT",
            interval="5m",
            action=SignalAction.ENTER_LONG,
            direction=SignalDirection.LONG,
            confidence=Decimal("1"),
            reason="entry",
            timestamp_ms=0,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            take_profit=Decimal("104"),
            metadata={"atr_dynamic_exits_enabled": True},
        )
        decision = RiskDecision(
            approved=True,
            reason="ok",
            signal=signal,
            position_size=Decimal("10"),
        )
        entry = broker.execute_decision(
            decision,
            market_price=Decimal("100"),
            timestamp_ms=0,
        )
        self.assertTrue(entry.accepted, entry.reason)

        broker.update_dynamic_atr_exits(
            _candle(high=Decimal("106"), low=Decimal("101"), close=Decimal("105")),
            atr=Decimal("2"),
            stop_multiple=Decimal("10"),
            take_profit_multiple=Decimal("2"),
            trailing_enabled=False,
            bb_band=BollingerBandPoint(
                middle=Decimal("104"),
                upper=Decimal("110"),
                lower=Decimal("98"),
                width_pct=Decimal("12"),
            ),
            bb_trail_enabled=True,
            bb_trail_partial_close_at_tp=True,
            bb_trail_partial_close_pct=Decimal("0.60"),
        )
        position = broker.open_positions()[0]
        self.assertTrue(position.metadata["bb_trail_active"])
        self.assertTrue(position.metadata["tp_is_partial_close"])

        reports = broker.process_candle(
            _candle(
                open_price=Decimal("103"),
                high=Decimal("104.5"),
                low=Decimal("103"),
                close=Decimal("104.2"),
                timestamp_ms=600_000,
            )
        )
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].reason, "bb_trail_partial_tp")
        self.assertEqual(reports[0].fill.quantity, Decimal("6"))  # type: ignore[union-attr]
        remaining = broker.open_positions()[0]
        self.assertEqual(remaining.quantity, Decimal("4"))
        self.assertTrue(remaining.metadata["bb_partial_close_executed"])

        second_reports = broker.process_candle(
            _candle(
                open_price=Decimal("104.2"),
                high=Decimal("105"),
                low=Decimal("103.5"),
                close=Decimal("104.8"),
                timestamp_ms=900_000,
            )
        )
        self.assertEqual(second_reports, [])
        self.assertEqual(broker.open_positions()[0].quantity, Decimal("4"))


if __name__ == "__main__":
    unittest.main()
