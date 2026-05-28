from __future__ import annotations

import unittest
from decimal import Decimal

from app.config import RiskSettings
from app.risk.manager import RiskManager
from app.risk.models import InstrumentMetadata, OpenPosition, RiskContext
from app.strategies.base import SignalAction, SignalDirection, StrategySignal


def _entry_signal(
    *,
    action: SignalAction = SignalAction.ENTER_LONG,
    direction: SignalDirection = SignalDirection.LONG,
    entry_price: Decimal = Decimal("100"),
    stop_loss: Decimal | None = Decimal("95"),
    take_profit: Decimal | None = Decimal("110"),
    metadata: dict[str, object] | None = None,
) -> StrategySignal:
    return StrategySignal(
        strategy_name="test",
        pair="B-BTC_USDT",
        interval="1h",
        action=action,
        direction=direction,
        confidence=Decimal("0.75"),
        reason="test signal",
        timestamp_ms=1,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        metadata=metadata or {},
    )


def _manager() -> RiskManager:
    return RiskManager(
        RiskSettings(
            max_risk_per_trade_pct=Decimal("1"),
            max_daily_loss_pct=Decimal("3"),
            max_open_positions=1,
            max_leverage=2,
        )
    )


class RiskManagerTests(unittest.TestCase):
    def test_approves_entry_and_sizes_fixed_fraction_risk(self) -> None:
        manager = _manager()
        decision = manager.evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("1000"),
            requested_leverage=Decimal("1"),
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.status.value, "APPROVED")
        self.assertEqual(decision.position_size, Decimal("2"))
        self.assertEqual(decision.notional, Decimal("200"))
        self.assertEqual(decision.max_loss, Decimal("10"))
        self.assertEqual(decision.leverage, Decimal("1"))

    def test_signal_risk_multiplier_reduces_actual_position_size(self) -> None:
        decision = _manager().evaluate_signal(
            _entry_signal(
                metadata={
                    "risk_multiplier": Decimal("0.5"),
                    "risk_multiplier_applies": True,
                }
            ),
            account_equity=Decimal("1000"),
            requested_leverage=Decimal("1"),
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.position_size, Decimal("1.0"))
        self.assertEqual(decision.max_loss, Decimal("5.0"))
        self.assertEqual(decision.metadata["risk_multiplier"], Decimal("0.5"))
        self.assertEqual(decision.metadata["risk_percent_used"], Decimal("0.50"))

    def test_rejects_entry_without_stop_loss(self) -> None:
        decision = _manager().evaluate_signal(
            _entry_signal(stop_loss=None),
            account_equity=Decimal("1000"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("stop loss", decision.reason)

    def test_rejects_entry_after_daily_loss_limit_is_reached(self) -> None:
        decision = _manager().evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("1000"),
            daily_realized_pnl=Decimal("-30"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("Max daily loss reached", decision.reason)

    def test_daily_loss_limit_can_use_day_start_equity(self) -> None:
        decision = _manager().evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("500"),
            daily_realized_pnl=Decimal("-25"),
            daily_loss_limit_equity=Decimal("1000"),
            requested_leverage=Decimal("1"),
        )

        self.assertTrue(decision.approved)

    def test_rejects_entry_when_max_open_positions_reached(self) -> None:
        position = OpenPosition(
            pair="B-ETH_USDT",
            direction=SignalDirection.LONG,
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
        )
        decision = _manager().evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("1000"),
            open_positions=[position],
        )

        self.assertFalse(decision.approved)
        self.assertIn("max_open_positions_blocked", decision.reason)

    def test_caps_same_position_scale_in_to_basket_risk_budget(self) -> None:
        position = OpenPosition(
            pair="B-BTC_USDT",
            direction=SignalDirection.LONG,
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
        )
        decision = _manager().evaluate_signal(
            _entry_signal(
                metadata={
                    "allow_scale_in": True,
                    "scale_in_reuses_position": True,
                }
            ),
            account_equity=Decimal("1000"),
            open_positions=[position],
            requested_leverage=Decimal("1"),
        )

        self.assertTrue(decision.approved)
        # Budget 10. Existing risk 5. Remaining budget 5.
        # Signal entry 100, SL 95 -> distance 5. 
        # Size = 5 / 5 = 1.
        self.assertEqual(decision.position_size, Decimal("1.00"))

        self.assertTrue(decision.metadata["basket_risk_checked"])
        self.assertTrue(decision.metadata["position_size_adjusted_for_basket_risk"])

    def test_rejects_scale_in_at_global_cap_without_reuse_flag(self) -> None:
        position = OpenPosition(
            pair="B-BTC_USDT",
            direction=SignalDirection.LONG,
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
        )
        manager = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("1"),
                max_daily_loss_pct=Decimal("3"),
                max_open_positions=1,
                max_open_positions_per_pair=2,
                max_leverage=2,
            )
        )
        decision = manager.evaluate_signal(
            _entry_signal(metadata={"allow_scale_in": True}),
            account_equity=Decimal("1000"),
            open_positions=[position],
            requested_leverage=Decimal("1"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("max_open_positions_blocked", decision.reason)

    def test_rejects_same_position_entry_without_scale_in_flag(self) -> None:
        position = OpenPosition(
            pair="B-BTC_USDT",
            direction=SignalDirection.LONG,
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
        )
        decision = _manager().evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("1000"),
            open_positions=[position],
            requested_leverage=Decimal("1"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("same_pair_position_blocked", decision.reason)

    def test_rejects_leverage_above_configured_limit(self) -> None:
        decision = _manager().evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("1000"),
            requested_leverage=Decimal("3"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("leverage exceeds", decision.reason)

    def test_rejects_leverage_above_instrument_limit(self) -> None:
        instrument = InstrumentMetadata(pair="B-BTC_USDT", max_leverage=Decimal("1.5"))
        decision = _manager().evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("1000"),
            instrument=instrument,
            requested_leverage=Decimal("2"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("leverage exceeds", decision.reason)

    def test_instrument_metadata_parses_nested_coindcx_shape(self) -> None:
        instrument = InstrumentMetadata.from_mapping(
            "B-BTC_USDT",
            {
                "instrument": {
                    "quantity_increment": 0.001,
                    "min_quantity": 0.001,
                    "min_notional": 60,
                    "max_leverage_long": 20,
                    "max_leverage_short": 10,
                    "price_increment": 0.1,
                }
            },
        )

        self.assertEqual(instrument.quantity_step, Decimal("0.001"))
        self.assertEqual(instrument.min_quantity, Decimal("0.001"))
        self.assertEqual(instrument.min_notional, Decimal("60"))
        self.assertEqual(instrument.max_leverage, Decimal("10"))
        self.assertEqual(instrument.tick_size, Decimal("0.1"))

    def test_rejects_take_profit_on_wrong_side(self) -> None:
        decision = _manager().evaluate_signal(
            _entry_signal(take_profit=Decimal("99")),
            account_equity=Decimal("1000"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("take profit", decision.reason)

    def test_caps_position_size_to_requested_leverage(self) -> None:
        decision = _manager().evaluate_signal(
            _entry_signal(stop_loss=Decimal("99.5")),
            account_equity=Decimal("1000"),
            requested_leverage=Decimal("1"),
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.notional, Decimal("1000"))
        self.assertEqual(decision.position_size, Decimal("10"))
        self.assertEqual(decision.max_loss, Decimal("5.0"))
        self.assertTrue(decision.metadata["capped_by_leverage"])

    def test_respects_instrument_quantity_step(self) -> None:
        instrument = InstrumentMetadata(
            pair="B-BTC_USDT",
            quantity_step=Decimal("0.3"),
        )
        decision = _manager().evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("1000"),
            instrument=instrument,
            requested_leverage=Decimal("1"),
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.position_size, Decimal("1.8"))
        self.assertEqual(decision.max_loss, Decimal("9.0"))

    def test_normalizes_entry_prices_to_instrument_tick_size(self) -> None:
        instrument = InstrumentMetadata(
            pair="B-BTC_USDT",
            tick_size=Decimal("0.1"),
        )
        decision = _manager().evaluate_signal(
            _entry_signal(
                entry_price=Decimal("100.03"),
                stop_loss=Decimal("95.08"),
                take_profit=Decimal("110.09"),
            ),
            account_equity=Decimal("1000"),
            instrument=instrument,
            requested_leverage=Decimal("1"),
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.signal.entry_price, Decimal("100.1"))
        self.assertEqual(decision.signal.stop_loss, Decimal("95.0"))
        self.assertEqual(decision.signal.take_profit, Decimal("110.1"))
        self.assertTrue(decision.signal.metadata["price_tick_normalized"])

    def test_normalizes_short_take_profit_away_from_entry(self) -> None:
        instrument = InstrumentMetadata(
            pair="B-BTC_USDT",
            tick_size=Decimal("0.1"),
        )
        decision = _manager().evaluate_signal(
            _entry_signal(
                action=SignalAction.ENTER_SHORT,
                direction=SignalDirection.SHORT,
                entry_price=Decimal("100.03"),
                stop_loss=Decimal("105.08"),
                take_profit=Decimal("90.09"),
            ),
            account_equity=Decimal("1000"),
            instrument=instrument,
            requested_leverage=Decimal("1"),
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.signal.entry_price, Decimal("100.0"))
        self.assertEqual(decision.signal.stop_loss, Decimal("105.1"))
        self.assertEqual(decision.signal.take_profit, Decimal("90.0"))

    def test_rejects_stop_too_close_to_estimated_liquidation(self) -> None:
        manager = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("1"),
                max_daily_loss_pct=Decimal("3"),
                max_open_positions=1,
                max_leverage=20,
                liquidation_buffer_pct=Decimal("1"),
            )
        )

        decision = manager.evaluate_signal(
            _entry_signal(entry_price=Decimal("100"), stop_loss=Decimal("88")),
            account_equity=Decimal("1000"),
            requested_leverage=Decimal("10"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("liquidation", decision.reason)

    def test_rejects_projected_notional_above_total_exposure_limit(self) -> None:
        manager = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("1"),
                max_daily_loss_pct=Decimal("3"),
                max_open_positions=2,
                max_leverage=2,
                max_total_open_notional_pct=Decimal("50"),
            )
        )
        position = OpenPosition(
            pair="B-ETH_USDT",
            direction=SignalDirection.LONG,
            quantity=Decimal("4.5"),
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
        )

        decision = manager.evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("1000"),
            open_positions=[position],
            requested_leverage=Decimal("1"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("Total open notional exceeds limit", decision.reason)

    def test_rejects_projected_risk_above_total_risk_limit(self) -> None:
        manager = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("1"),
                max_daily_loss_pct=Decimal("3"),
                max_open_positions=2,
                max_leverage=2,
                max_total_risk_pct=Decimal("1.4"),
            )
        )
        position = OpenPosition(
            pair="B-ETH_USDT",
            direction=SignalDirection.LONG,
            quantity=Decimal("3"),
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
        )

        decision = manager.evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("1000"),
            open_positions=[position],
            requested_leverage=Decimal("1"),
        )

        self.assertFalse(decision.approved)
        self.assertIn("basket_risk_blocked", decision.reason)

    def test_sizes_from_available_equity_when_it_is_lower(self) -> None:
        manager = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("1"),
                max_daily_loss_pct=Decimal("3"),
                max_open_positions=2,
                max_leverage=10,
            )
        )

        decision = manager.evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("1000"),
            available_equity=Decimal("500"),
            requested_leverage=Decimal("1"),
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.max_loss, Decimal("5"))
        self.assertEqual(decision.metadata["risk_base_amount"], Decimal("500"))
        self.assertTrue(decision.metadata["risk_base_capped_by_available_equity"])

    def test_correlated_same_direction_basket_reduces_risk(self) -> None:
        manager = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("2"),
                max_daily_loss_pct=Decimal("3"),
                max_open_positions=3,
                max_leverage=10,
                max_total_risk_pct=Decimal("20"),
            )
        )
        position = OpenPosition(
            pair="B-ETH_USDT",
            direction=SignalDirection.LONG,
            quantity=Decimal("1"),
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
        )

        decision = manager.evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("1000"),
            open_positions=[position],
            requested_leverage=Decimal("1"),
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.metadata["basket_risk_multiplier"], Decimal("0.5"))
        self.assertEqual(decision.metadata["risk_percent_used"], Decimal("1.00"))

    def test_approves_exit_signal_without_new_position_sizing(self) -> None:
        signal = StrategySignal(
            strategy_name="test",
            pair="B-BTC_USDT",
            interval="1h",
            action=SignalAction.EXIT_LONG,
            direction=SignalDirection.LONG,
            confidence=Decimal("0.6"),
            reason="exit",
            timestamp_ms=1,
        )

        decision = _manager().evaluate_signal(
            signal,
            account_equity=Decimal("1000"),
            daily_realized_pnl=Decimal("-30"),
        )

        self.assertTrue(decision.approved)
        self.assertIsNone(decision.position_size)
        self.assertIsNone(decision.max_loss)

    def test_rejects_live_trading_context_by_default(self) -> None:
        decision = _manager().evaluate(
            RiskContext(
                signal=_entry_signal(),
                account_equity=Decimal("1000"),
                trading_mode="live",
                live_trading_enabled=True,
            )
        )

        self.assertFalse(decision.approved)
        self.assertIn("Live risk approval is disabled", decision.reason)

    def test_live_pilot_approval_uses_strict_caps(self) -> None:
        manager = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("0.5"),
                max_daily_loss_pct=Decimal("2"),
                max_open_positions=1,
                max_leverage=3,
                live_risk_approval_enabled=True,
                live_max_order_notional=Decimal("1000"),
                live_max_margin_per_order=Decimal("500"),
            )
        )

        decision = manager.evaluate(
            RiskContext(
                signal=_entry_signal(),
                account_equity=Decimal("1000"),
                requested_leverage=Decimal("1"),
                trading_mode="live",
                live_trading_enabled=True,
            )
        )

        self.assertTrue(decision.approved)
        self.assertFalse(decision.metadata["paper_only"])
        self.assertTrue(decision.metadata["live_pilot"])

    def test_live_pilot_blocks_order_above_absolute_notional_cap(self) -> None:
        manager = RiskManager(
            RiskSettings(
                max_risk_per_trade_pct=Decimal("1"),
                max_daily_loss_pct=Decimal("2"),
                max_open_positions=1,
                max_leverage=3,
                live_risk_approval_enabled=True,
                live_max_order_notional=Decimal("100"),
                live_max_margin_per_order=Decimal("500"),
            )
        )

        decision = manager.evaluate(
            RiskContext(
                signal=_entry_signal(),
                account_equity=Decimal("1000"),
                requested_leverage=Decimal("1"),
                trading_mode="live",
                live_trading_enabled=True,
            )
        )

        self.assertFalse(decision.approved)
        self.assertIn("live_notional_cap_blocked", decision.reason)

    def test_decision_serializes_nested_decimal_values(self) -> None:
        manager = _manager()
        decision = manager.evaluate_signal(
            _entry_signal(),
            account_equity=Decimal("1000"),
        )
        data = decision.to_dict()

        self.assertEqual(data["status"], "APPROVED")
        self.assertEqual(data["position_size"], "2.00")
        self.assertEqual(data["signal"]["confidence"], "0.75")
        self.assertEqual(data["metadata"]["risk_budget"], "10.00")


if __name__ == "__main__":
    unittest.main()
