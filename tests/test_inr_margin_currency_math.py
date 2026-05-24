from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from decimal import Decimal

from app.broker.paper import PaperBroker
from app.persistence.paper_state import PaperStateStore
from app.config import RiskSettings
from app.risk.manager import RiskManager
from app.strategies.base import SignalAction, SignalDirection, StrategySignal


def _signal(
    *,
    direction: SignalDirection = SignalDirection.LONG,
    entry: Decimal = Decimal("0.60"),
    stop: Decimal = Decimal("0.54"),
    target: Decimal = Decimal("0.72"),
) -> StrategySignal:
    return StrategySignal(
        strategy_name="currency-test",
        pair="B-BSB_USDT",
        interval="5m",
        action=(
            SignalAction.ENTER_LONG
            if direction == SignalDirection.LONG
            else SignalAction.ENTER_SHORT
        ),
        direction=direction,
        confidence=Decimal("0.8"),
        reason="test",
        timestamp_ms=1,
        entry_price=entry,
        stop_loss=stop,
        take_profit=target,
    )


def _exit(direction: SignalDirection, price: Decimal) -> StrategySignal:
    return StrategySignal(
        strategy_name="currency-test",
        pair="B-BSB_USDT",
        interval="5m",
        action=(
            SignalAction.EXIT_LONG
            if direction == SignalDirection.LONG
            else SignalAction.EXIT_SHORT
        ),
        direction=direction,
        confidence=Decimal("0.8"),
        reason="exit",
        timestamp_ms=2,
        entry_price=price,
    )


class InrMarginCurrencyMathTests(unittest.TestCase):
    def test_risk_sizing_converts_usdt_price_distance_to_inr_risk(self) -> None:
        decision = RiskManager(
            RiskSettings(max_risk_per_trade_pct=Decimal("3"), max_leverage=Decimal("5"))
        ).evaluate_signal(
            _signal(),
            account_equity=Decimal("10000"),
            requested_leverage=Decimal("5"),
            quote_to_margin_rate=Decimal("98"),
        )

        self.assertTrue(decision.approved, decision.reason)
        self.assertEqual(decision.max_loss, Decimal("300.0000000000000000000000000"))
        self.assertEqual(decision.position_size, Decimal("51.02040816326530612244897959"))
        self.assertEqual(decision.notional, Decimal("3000.000000000000000000000000"))
        self.assertEqual(decision.metadata["quote_to_margin_rate"], Decimal("98"))

    def test_paper_broker_reports_pnl_fees_and_notional_in_inr(self) -> None:
        broker = PaperBroker(
            starting_equity=Decimal("10000"),
            maker_fee_rate=Decimal("0.0002"),
            taker_fee_rate=Decimal("0.0005"),
            fee_gst_rate=Decimal("0.18"),
            entry_fee_type="maker",
            exit_fee_type="taker",
            quote_to_margin_rate=Decimal("98"),
            account_currency="INR",
            price_quote_currency="USDT",
        )
        risk = RiskManager(
            RiskSettings(max_risk_per_trade_pct=Decimal("3"), max_leverage=Decimal("5"))
        )
        entry = _signal()
        decision = risk.evaluate_signal(
            entry,
            account_equity=Decimal("10000"),
            requested_leverage=Decimal("5"),
            quote_to_margin_rate=Decimal("98"),
        )
        entry_report = broker.execute_decision(
            decision,
            market_price=Decimal("0.60"),
            timestamp_ms=1,
        )

        self.assertTrue(entry_report.accepted, entry_report.reason)
        self.assertEqual(entry_report.position.quantity, Decimal("51.02040816326530612244897959"))
        self.assertEqual(entry_report.position.notional, Decimal("3000.000000000000000000000000"))
        self.assertEqual(entry_report.fill.metadata["notional_margin"], Decimal("3000.000000000000000000000000"))
        self.assertEqual(entry_report.fill.fee, Decimal("0.7080000000000000000000000000"))

        exit_report = broker.execute_decision(
            risk.evaluate_signal(_exit(SignalDirection.LONG, Decimal("0.66")), account_equity=Decimal("10000")),
            market_price=Decimal("0.66"),
            timestamp_ms=2,
        )

        self.assertTrue(exit_report.accepted, exit_report.reason)
        self.assertEqual(exit_report.fill.realized_pnl, Decimal("300.0000000000000000000000000"))
        self.assertEqual(exit_report.fill.metadata["notional_margin"], Decimal("3300.000000000000000000000000"))
        self.assertEqual(exit_report.fill.fee, Decimal("1.947000000000000000000000000"))
        self.assertEqual(broker.realized_pnl, Decimal("300.0000000000000000000000000"))
        self.assertEqual(broker.fees_paid, Decimal("2.655000000000000000000000000"))

    def test_broker_does_not_restore_legacy_quote_currency_state_into_inr_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper_state.db"
            store = PaperStateStore(str(db_path))
            store.save(
                {
                    "equity": "10100",
                    "positions": {},
                    "fills": [
                        {
                            "fill_id": "legacy-fill",
                            "order_id": "legacy-order",
                            "pair": "B-BSB_USDT",
                            "side": "buy",
                            "quantity": "1000",
                            "price": "0.50",
                            "fee": "0.25",
                            "timestamp_ms": 1,
                            "realized_pnl": "100",
                            "metadata": {"fee_type": "taker"},
                        }
                    ],
                    "realized_pnl": "100",
                    "fees_paid": "0.25",
                    "funding_paid": "0",
                }
            )

            broker = PaperBroker(
                starting_equity=Decimal("10000"),
                quote_to_margin_rate=Decimal("98"),
                account_currency="INR",
                price_quote_currency="USDT",
                state_store=store,
            )

            self.assertEqual(broker.fills, [])
            self.assertEqual(broker.realized_pnl, Decimal("0"))
            self.assertEqual(broker.fees_paid, Decimal("0"))
            store.close()


if __name__ == "__main__":
    unittest.main()
