from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from app.broker.models import PaperPosition
from app.strategies.base import SignalDirection
from app.strategies.bollinger_trail_policy import (
    advance_stage,
    compute_candidate_stop,
    ratchet_stop,
    should_activate,
    update_bb_trail,
)


def _position(
    *,
    direction: SignalDirection = SignalDirection.LONG,
    metadata: dict | None = None,
) -> PaperPosition:
    return PaperPosition(
        pair="B-BTC_USDT",
        direction=direction,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        leverage=Decimal("1"),
        opened_at_ms=0,
        updated_at_ms=0,
        strategy_name="test",
        stop_loss=Decimal("95"),
        take_profit=Decimal("115"),
        metadata=metadata or {},
    )


class BollingerTrailPolicyTests(unittest.TestCase):
    def test_should_not_activate_before_activation_r(self) -> None:
        self.assertFalse(
            should_activate(
                _position(),
                Decimal("0.3"),
                Decimal("0.5"),
                Decimal("105"),
            )
        )

    def test_should_not_activate_if_middle_band_below_entry_for_long(self) -> None:
        self.assertFalse(
            should_activate(
                _position(),
                Decimal("1.0"),
                Decimal("0.5"),
                Decimal("99"),
            )
        )

    def test_activates_when_r_met_and_middle_band_confirms(self) -> None:
        self.assertTrue(
            should_activate(
                _position(),
                Decimal("0.6"),
                Decimal("0.5"),
                Decimal("101"),
            )
        )

    def test_ratchet_never_loosens_for_long(self) -> None:
        self.assertEqual(
            ratchet_stop(Decimal("100"), Decimal("98"), SignalDirection.LONG),
            Decimal("100"),
        )

    def test_ratchet_advances_for_long(self) -> None:
        self.assertEqual(
            ratchet_stop(Decimal("100"), Decimal("102"), SignalDirection.LONG),
            Decimal("102"),
        )

    def test_ratchet_never_loosens_for_short(self) -> None:
        self.assertEqual(
            ratchet_stop(Decimal("100"), Decimal("103"), SignalDirection.SHORT),
            Decimal("100"),
        )

    def test_stage_never_decreases(self) -> None:
        self.assertEqual(
            advance_stage(
                current_stage=2,
                current_r=Decimal("0.3"),
                stage2_r=Decimal("0.5"),
                stage3_r=Decimal("1.0"),
            ),
            2,
        )

    def test_squeeze_guard_no_update(self) -> None:
        position = _position(
            metadata={
                "bb_trail_active": True,
                "bb_trail_stop": Decimal("100"),
                "bb_trail_stage": 2,
                "atr_best_price": Decimal("110"),
            }
        )
        meta = update_bb_trail(
            position,
            Decimal("1.0"),
            Decimal("101"),
            Decimal("101.2"),
            Decimal("100.9"),
            Decimal("1"),
            Decimal("1.0"),
            Decimal("0.5"),
            Decimal("0.5"),
            Decimal("1.0"),
            Decimal("4.0"),
        )

        self.assertEqual(meta["bb_trail_stop"], Decimal("100"))

    def test_force_close_flag_set_at_ceiling_r(self) -> None:
        meta = update_bb_trail(
            _position(),
            Decimal("4.1"),
            Decimal("105"),
            Decimal("110"),
            Decimal("100"),
            Decimal("2"),
            Decimal("1.0"),
            Decimal("0.5"),
            Decimal("0.5"),
            Decimal("1.0"),
            Decimal("4.0"),
        )

        self.assertTrue(meta["bb_trail_force_close"])

    def test_buffer_computation_long(self) -> None:
        self.assertEqual(
            compute_candidate_stop(
                Decimal("100"),
                Decimal("2"),
                Decimal("1.0"),
                SignalDirection.LONG,
            ),
            Decimal("98.0"),
        )

    def test_buffer_computation_short(self) -> None:
        self.assertEqual(
            compute_candidate_stop(
                Decimal("100"),
                Decimal("2"),
                Decimal("1.0"),
                SignalDirection.SHORT,
            ),
            Decimal("102.0"),
        )

    def test_update_returns_all_required_keys(self) -> None:
        meta = update_bb_trail(
            _position(),
            Decimal("0.6"),
            Decimal("102"),
            Decimal("110"),
            Decimal("94"),
            Decimal("2"),
            Decimal("1.0"),
            Decimal("0.5"),
            Decimal("0.5"),
            Decimal("1.0"),
            Decimal("4.0"),
        )

        self.assertTrue(
            {
                "bb_trail_enabled",
                "bb_trail_active",
                "bb_trail_stop",
                "bb_trail_stage",
                "bb_trail_best_price",
                "bb_mid",
                "bb_upper",
                "bb_lower",
                "bb_buffer_atr",
                "bb_trail_force_close",
                "stop_type",
            }.issubset(meta)
        )

    def test_full_lifecycle_long_position(self) -> None:
        position = _position(metadata={"atr_best_price": Decimal("100")})
        stops: list[Decimal] = []
        stages: list[int] = []
        for current_r, middle in (
            (Decimal("0.1"), Decimal("100.5")),
            (Decimal("0.3"), Decimal("101")),
            (Decimal("0.5"), Decimal("103")),
            (Decimal("0.7"), Decimal("104")),
            (Decimal("1.0"), Decimal("105")),
            (Decimal("1.4"), Decimal("106")),
            (Decimal("1.8"), Decimal("107")),
            (Decimal("2.2"), Decimal("108")),
            (Decimal("2.6"), Decimal("109")),
            (Decimal("3.0"), Decimal("110")),
        ):
            meta = update_bb_trail(
                position,
                current_r,
                middle,
                middle + Decimal("5"),
                middle - Decimal("5"),
                Decimal("2"),
                Decimal("1.0"),
                Decimal("0.5"),
                Decimal("0.5"),
                Decimal("1.0"),
                Decimal("4.0"),
            )
            position = replace(position, metadata={**position.metadata, **meta})
            if meta["bb_trail_stop"] is not None:
                stops.append(meta["bb_trail_stop"])
            stages.append(meta["bb_trail_stage"])

        self.assertEqual(stops, sorted(stops))
        self.assertEqual(stages, sorted(stages))
        self.assertEqual(stages[-1], 3)


if __name__ == "__main__":
    unittest.main()
