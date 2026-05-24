from __future__ import annotations

import unittest
from decimal import Decimal

from app.config import RiskSettings
from app.risk.manager import RiskManager
from app.risk.pair_performance import pair_recent_risk_profile
from app.strategies.base import SignalAction, SignalDirection, StrategySignal


def _entry_signal(
    *,
    entry_price: Decimal = Decimal("100"),
    stop_loss: Decimal = Decimal("95"),
    metadata: dict[str, object] | None = None,
) -> StrategySignal:
    return StrategySignal(
        strategy_name="test",
        pair="B-ALICE_USDT",
        interval="5m",
        action=SignalAction.ENTER_LONG,
        direction=SignalDirection.LONG,
        confidence=Decimal("0.8"),
        reason="test",
        timestamp_ms=1,
        entry_price=entry_price,
        stop_loss=stop_loss,
        metadata=metadata or {},
    )


class EntrySafetyTests(unittest.TestCase):
    def test_rejects_tiny_stop_distance_before_sizing(self) -> None:
        decision = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("4"),
                max_leverage=10,
                min_stop_distance_pct=Decimal("0.50"),
            )
        ).evaluate_signal(
            _entry_signal(stop_loss=Decimal("99.70")),
            account_equity=Decimal("100000"),
            requested_leverage=Decimal("5"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("entry_stop_distance_too_small", decision.reason)

    def test_rejects_low_atr_regime_when_atr_metadata_is_available(self) -> None:
        decision = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("4"),
                max_leverage=10,
                min_entry_atr_pct=Decimal("0.10"),
            )
        ).evaluate_signal(
            _entry_signal(
                stop_loss=Decimal("99"),
                metadata={"atr_entry_atr": Decimal("0.05")},
            ),
            account_equity=Decimal("100000"),
            requested_leverage=Decimal("5"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("entry_atr_too_low", decision.reason)

    def test_rejects_low_volume_liquidity(self) -> None:
        decision = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("4"),
                max_leverage=10,
                min_entry_volume_ratio=Decimal("0.50"),
            )
        ).evaluate_signal(
            _entry_signal(metadata={"visual": {"volume_ratio": Decimal("0.20")}}),
            account_equity=Decimal("100000"),
            requested_leverage=Decimal("5"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("entry_liquidity_volume_too_low", decision.reason)

    def test_rejects_wide_orderbook_spread(self) -> None:
        decision = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("4"),
                max_leverage=10,
                max_entry_spread_pct=Decimal("0.30"),
            )
        ).evaluate_signal(
            _entry_signal(metadata={"entry_orderbook_spread_pct": Decimal("0.45")}),
            account_equity=Decimal("100000"),
            requested_leverage=Decimal("5"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("entry_spread_too_wide", decision.reason)

    def test_rejects_per_trade_margin_cap(self) -> None:
        decision = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("4"),
                max_leverage=10,
                max_margin_per_trade_pct=Decimal("25"),
            )
        ).evaluate_signal(
            _entry_signal(entry_price=Decimal("100"), stop_loss=Decimal("99")),
            account_equity=Decimal("100000"),
            available_equity=Decimal("100000"),
            requested_leverage=Decimal("5"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("per_trade_margin_blocked", decision.reason)

    def test_pair_recent_losses_reduce_risk_multiplier(self) -> None:
        profile = pair_recent_risk_profile(
            [Decimal("-10"), Decimal("5"), Decimal("-2")],
            RiskSettings(
                pair_loss_lookback=4,
                pair_loss_limit=2,
                pair_loss_risk_multiplier=Decimal("0.50"),
            ),
        )

        self.assertEqual(profile.multiplier, Decimal("0.50"))
        self.assertTrue(profile.metadata["pair_risk_throttle_active"])

    def test_pair_severe_recent_losses_reduce_risk_more(self) -> None:
        profile = pair_recent_risk_profile(
            [Decimal("-10"), Decimal("-5"), Decimal("-2")],
            RiskSettings(
                pair_loss_lookback=4,
                pair_loss_limit=2,
                pair_loss_risk_multiplier=Decimal("0.50"),
                pair_loss_severe_limit=3,
                pair_loss_severe_risk_multiplier=Decimal("0.25"),
            ),
        )

        self.assertEqual(profile.multiplier, Decimal("0.25"))


if __name__ == "__main__":
    unittest.main()
