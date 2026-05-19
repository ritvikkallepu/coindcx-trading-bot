from __future__ import annotations

import logging
import unittest
from decimal import Decimal

from app.data.candle_builder import CandleSeries, OHLCVCandle
from app.data.indicators import latest_indicator_snapshot
from app.strategies.adaptive_hybrid import AdaptiveHybridStrategy
from app.strategies.base import SignalAction, SignalDirection, Strategy, StrategyContext, StrategyEngine
from app.strategies.bb_dynamic_grid import BollingerDynamicFuturesGridStrategy
from app.strategies.bb_volume_reversion import BollingerVolumeMeanReversionStrategy
from app.strategies.ema_rsi_trend import EMARSICrossoverStrategy
from app.strategies.atr_policy import ATRPolicyRouter
from app.strategies.defaults import strategy_engine_for_name
from app.strategies.hybrid_meta import HybridMetaStrategy, HybridMetaV2Strategy


def _series_from_closes(
    closes: list[Decimal],
    *,
    interval: str = "1h",
    base_volume: Decimal = Decimal("100"),
    last_volume: Decimal | None = None,
) -> CandleSeries:
    candles: list[OHLCVCandle] = []
    for index, close in enumerate(closes):
        volume = last_volume if index == len(closes) - 1 and last_volume is not None else base_volume
        candles.append(
            OHLCVCandle(
                pair="B-BTC_USDT",
                interval=interval,
                open_time_ms=index * 3_600_000,
                close_time_ms=((index + 1) * 3_600_000) - 1,
                open=close,
                high=close + Decimal("0.5"),
                low=close - Decimal("0.5"),
                close=close,
                volume=volume,
            )
        )
    return CandleSeries(candles)


def _context(
    series: CandleSeries,
    *,
    features: dict[str, object] | None = None,
) -> StrategyContext:
    latest = series.latest()
    return StrategyContext(
        pair="B-BTC_USDT",
        interval=latest.interval if latest is not None else "1h",
        candles=series,
        indicators=latest_indicator_snapshot(series),
        features=features or {},
    )


class StrategyTests(unittest.TestCase):
    def test_atr_policy_router_uses_runner_for_breakout_setup(self) -> None:
        policy = ATRPolicyRouter().select(
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            atr=Decimal("2"),
            metadata={
                "final_score": Decimal("0.7"),
                "ema_score": Decimal("0.5"),
                "bb_score": Decimal("0.2"),
                "visual_score": Decimal("0.4"),
                "open_interest_score": Decimal("0.1"),
                "visual": {"volume_ratio": Decimal("1.5")},
            },
        )

        self.assertEqual(policy.atr_profile, "breakout_runner")
        self.assertTrue(policy.atr_stop_enabled)
        self.assertFalse(policy.atr_take_profit_enabled)
        self.assertTrue(policy.atr_trailing_enabled)

    def test_signal_dict_serializes_decimal_and_enum_values(self) -> None:
        series = _series_from_closes([Decimal("100")] * 30)
        signal = EMARSICrossoverStrategy().evaluate(_context(series))
        data = signal.to_dict()

        self.assertIsInstance(data["action"], str)
        self.assertIsInstance(data["confidence"], str)

    def test_ema_rsi_strategy_emits_bullish_entry_on_cross_up(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("99"),
            Decimal("98"),
            Decimal("97"),
            Decimal("96"),
            Decimal("97"),
            Decimal("105"),
        ]
        strategy = EMARSICrossoverStrategy(
            fast_period=2,
            slow_period=4,
            rsi_period=3,
            atr_period=3,
            long_rsi_max=Decimal("100"),
        )

        signal = strategy.evaluate(_context(_series_from_closes(closes)))

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertIsNotNone(signal.stop_loss)
        self.assertIsNotNone(signal.take_profit)
        self.assertLess(signal.stop_loss, signal.entry_price)  # type: ignore[operator]
        self.assertGreater(signal.take_profit, signal.entry_price)  # type: ignore[operator]

    def test_ema_rsi_strategy_emits_bearish_entry_on_cross_down(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("101"),
            Decimal("102"),
            Decimal("103"),
            Decimal("104"),
            Decimal("103"),
            Decimal("95"),
        ]
        strategy = EMARSICrossoverStrategy(
            fast_period=2,
            slow_period=4,
            rsi_period=3,
            atr_period=3,
            short_rsi_min=Decimal("0"),
        )

        signal = strategy.evaluate(_context(_series_from_closes(closes)))

        self.assertEqual(signal.action, SignalAction.ENTER_SHORT)
        self.assertGreater(signal.stop_loss, signal.entry_price)  # type: ignore[operator]
        self.assertLess(signal.take_profit, signal.entry_price)  # type: ignore[operator]

    def test_bollinger_volume_strategy_emits_long_reversion_signal(self) -> None:
        closes = [Decimal("100")] * 20 + [Decimal("95")]
        strategy = BollingerVolumeMeanReversionStrategy(
            bollinger_period=5,
            squeeze_lookback=10,
            squeeze_rank_threshold=Decimal("1"),
            volume_period=5,
            volume_multiplier=Decimal("1.1"),
            atr_period=5,
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    base_volume=Decimal("100"),
                    last_volume=Decimal("200"),
                )
            )
        )

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertLess(signal.stop_loss, signal.entry_price)  # type: ignore[operator]
        self.assertGreater(signal.take_profit, signal.entry_price)  # type: ignore[operator]

    def test_bollinger_volume_strategy_emits_short_reversion_signal(self) -> None:
        closes = [Decimal("100")] * 20 + [Decimal("105")]
        strategy = BollingerVolumeMeanReversionStrategy(
            bollinger_period=5,
            squeeze_lookback=10,
            squeeze_rank_threshold=Decimal("1"),
            volume_period=5,
            volume_multiplier=Decimal("1.1"),
            atr_period=5,
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    base_volume=Decimal("100"),
                    last_volume=Decimal("200"),
                )
            )
        )

        self.assertEqual(signal.action, SignalAction.ENTER_SHORT)
        self.assertGreater(signal.stop_loss, signal.entry_price)  # type: ignore[operator]
        self.assertLess(signal.take_profit, signal.entry_price)  # type: ignore[operator]

    def test_strategy_engine_turns_strategy_failure_into_hold(self) -> None:
        class BrokenStrategy(Strategy):
            name = "broken"

            def evaluate(self, context: StrategyContext):
                raise RuntimeError("boom")

        logger = logging.getLogger("test.strategy.engine")
        logger.disabled = True
        engine = StrategyEngine([BrokenStrategy()], logger=logger)
        signal = engine.evaluate(_context(_series_from_closes([Decimal("100")] * 30)))[0]

        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertEqual(signal.strategy_name, "broken")

    def test_hybrid_meta_emits_bullish_entry_when_scores_align(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("99"),
            Decimal("98"),
            Decimal("97"),
            Decimal("98"),
            Decimal("101"),
            Decimal("104"),
            Decimal("108"),
        ]
        strategy = HybridMetaStrategy(
            fast_period=2,
            slow_period=4,
            rsi_period=3,
            bollinger_period=5,
            atr_period=3,
            volume_period=3,
            visual_lookback=5,
            entry_threshold=Decimal("0.35"),
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    base_volume=Decimal("100"),
                    last_volume=Decimal("160"),
                )
            )
        )

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertEqual(signal.strategy_name, "hybrid_meta")
        self.assertIn("final_score", signal.metadata)
        self.assertEqual(signal.metadata["open_interest"]["source"], "not_configured")

    def test_hybrid_meta_visual_screen_blocks_low_volume(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("99"),
            Decimal("98"),
            Decimal("97"),
            Decimal("98"),
            Decimal("101"),
            Decimal("104"),
            Decimal("108"),
        ]
        strategy = HybridMetaStrategy(
            fast_period=2,
            slow_period=4,
            rsi_period=3,
            bollinger_period=5,
            atr_period=3,
            volume_period=3,
            visual_lookback=5,
            entry_threshold=Decimal("0.35"),
            min_volume_ratio=Decimal("0.5"),
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    base_volume=Decimal("100"),
                    last_volume=Decimal("10"),
                )
            )
        )

        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertIn("Visual screen blocked", signal.reason)

    def test_hybrid_meta_visual_screen_does_not_block_open_position_exit(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("101"),
            Decimal("102"),
            Decimal("103"),
            Decimal("102"),
            Decimal("99"),
            Decimal("96"),
            Decimal("92"),
        ]
        strategy = HybridMetaStrategy(
            fast_period=2,
            slow_period=4,
            rsi_period=3,
            bollinger_period=5,
            atr_period=3,
            volume_period=3,
            visual_lookback=5,
            entry_threshold=Decimal("0.25"),
            exit_threshold=Decimal("0.20"),
            min_volume_ratio=Decimal("0.5"),
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    base_volume=Decimal("100"),
                    last_volume=Decimal("10"),
                ),
                features={
                    "open_position": {
                        "pair": "B-BTC_USDT",
                        "direction": "long",
                        "entry_price": Decimal("100"),
                        "quantity": Decimal("1"),
                    }
                },
            )
        )

        self.assertEqual(signal.action, SignalAction.EXIT_LONG)
        self.assertNotIn("Visual screen blocked", signal.reason)
        self.assertTrue(signal.metadata["visual"]["blocked"])

    def test_hybrid_meta_ignores_open_interest_weight_when_score_unavailable(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("99"),
            Decimal("98"),
            Decimal("97"),
            Decimal("98"),
            Decimal("101"),
            Decimal("104"),
            Decimal("108"),
        ]
        strategy = HybridMetaStrategy(
            fast_period=2,
            slow_period=4,
            rsi_period=3,
            bollinger_period=5,
            atr_period=3,
            volume_period=3,
            visual_lookback=5,
            entry_threshold=Decimal("0.35"),
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    base_volume=Decimal("100"),
                    last_volume=Decimal("160"),
                ),
                features={"open_interest": {"source": "unit-test"}},
            )
        )

        self.assertEqual(signal.metadata["open_interest"]["score_used"], False)
        self.assertEqual(signal.metadata["active_weight"], Decimal("0.90"))

    def test_hybrid_meta_requires_open_interest_for_new_short(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("101"),
            Decimal("102"),
            Decimal("103"),
            Decimal("102"),
            Decimal("99"),
            Decimal("96"),
            Decimal("92"),
        ]
        strategy = HybridMetaStrategy(
            fast_period=2,
            slow_period=4,
            rsi_period=3,
            bollinger_period=5,
            atr_period=3,
            volume_period=3,
            visual_lookback=5,
            entry_threshold=Decimal("0.25"),
            exit_threshold=Decimal("0.25"),
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    base_volume=Decimal("100"),
                    last_volume=Decimal("160"),
                )
            )
        )

        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertIn("open-interest confirmation", signal.reason)

    def test_hybrid_meta_allows_new_short_with_open_interest_confirmation(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("101"),
            Decimal("102"),
            Decimal("103"),
            Decimal("102"),
            Decimal("99"),
            Decimal("96"),
            Decimal("92"),
        ]
        strategy = HybridMetaStrategy(
            fast_period=2,
            slow_period=4,
            rsi_period=3,
            bollinger_period=5,
            atr_period=3,
            volume_period=3,
            visual_lookback=5,
            entry_threshold=Decimal("0.25"),
            exit_threshold=Decimal("0.25"),
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    base_volume=Decimal("100"),
                    last_volume=Decimal("160"),
                ),
                features={
                    "open_interest": {
                        "source": "unit-test",
                        "score": "-1",
                        "change_pct": "-5",
                    }
                },
            )
        )

        self.assertEqual(signal.action, SignalAction.ENTER_SHORT)
        self.assertEqual(signal.direction, SignalDirection.SHORT)

    def test_hybrid_meta_exits_long_instead_of_reversing_when_position_is_open(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("101"),
            Decimal("102"),
            Decimal("103"),
            Decimal("102"),
            Decimal("99"),
            Decimal("96"),
            Decimal("92"),
        ]
        strategy = HybridMetaStrategy(
            fast_period=2,
            slow_period=4,
            rsi_period=3,
            bollinger_period=5,
            atr_period=3,
            volume_period=3,
            visual_lookback=5,
            entry_threshold=Decimal("0.25"),
            exit_threshold=Decimal("0.25"),
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    base_volume=Decimal("100"),
                    last_volume=Decimal("160"),
                ),
                features={
                    "open_position": {
                        "pair": "B-BTC_USDT",
                        "direction": "long",
                        "entry_price": Decimal("100"),
                        "quantity": Decimal("1"),
                    }
                },
            )
        )

        self.assertEqual(signal.action, SignalAction.EXIT_LONG)
        self.assertEqual(signal.direction, SignalDirection.LONG)

    def test_hybrid_meta_v2_is_separate_selectable_strategy(self) -> None:
        engine = strategy_engine_for_name("hybrid_meta_v2")

        self.assertIsInstance(engine.strategies[0], HybridMetaV2Strategy)
        self.assertEqual(engine.strategies[0].name, "hybrid_meta_v2")

    def test_adaptive_hybrid_uses_ema_primary_on_one_hour(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("99"),
            Decimal("98"),
            Decimal("97"),
            Decimal("96"),
            Decimal("97"),
            Decimal("105"),
        ]
        strategy = AdaptiveHybridStrategy(
            ema_strategy=EMARSICrossoverStrategy(
                fast_period=2,
                slow_period=4,
                rsi_period=3,
                atr_period=3,
                long_rsi_max=Decimal("100"),
            ),
            visual_filter=HybridMetaStrategy(
                fast_period=2,
                slow_period=4,
                rsi_period=3,
                bollinger_period=5,
                atr_period=3,
                volume_period=3,
                visual_lookback=5,
                min_volume_ratio=Decimal("0.5"),
            ),
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    base_volume=Decimal("100"),
                    last_volume=Decimal("160"),
                )
            )
        )

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertEqual(signal.strategy_name, "adaptive_hybrid")
        self.assertEqual(signal.metadata["primary_strategy"], "ema_rsi_trend")

    def test_adaptive_hybrid_can_use_ema_primary_on_four_hour_trend(self) -> None:
        closes = [Decimal("100")] * 24 + [
            Decimal("99"),
            Decimal("98"),
            Decimal("97"),
            Decimal("96"),
            Decimal("97"),
            Decimal("105"),
        ]
        strategy = AdaptiveHybridStrategy(
            ema_strategy=EMARSICrossoverStrategy(
                fast_period=2,
                slow_period=4,
                rsi_period=3,
                atr_period=3,
                long_rsi_max=Decimal("100"),
            ),
            bb_strategy=BollingerVolumeMeanReversionStrategy(
                bollinger_period=5,
                squeeze_lookback=10,
                volume_period=3,
                atr_period=3,
            ),
            visual_filter=HybridMetaStrategy(
                fast_period=2,
                slow_period=4,
                rsi_period=3,
                bollinger_period=5,
                atr_period=3,
                volume_period=3,
                visual_lookback=5,
                min_volume_ratio=Decimal("0.5"),
            ),
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    interval="4h",
                    base_volume=Decimal("100"),
                    last_volume=Decimal("160"),
                )
            )
        )

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertEqual(signal.metadata["primary_strategy"], "ema_rsi_trend")
        self.assertEqual(signal.metadata["market_regime"]["mode"], "trend")

    def test_adaptive_hybrid_can_use_bollinger_primary_on_one_hour_reversion(self) -> None:
        closes = [Decimal("100")] * 20 + [Decimal("95")]
        strategy = AdaptiveHybridStrategy(
            trend_regime_threshold=Decimal("2"),
            bb_strategy=BollingerVolumeMeanReversionStrategy(
                bollinger_period=5,
                squeeze_lookback=10,
                squeeze_rank_threshold=Decimal("1"),
                volume_period=5,
                volume_multiplier=Decimal("1.1"),
                atr_period=5,
            ),
            visual_filter=HybridMetaStrategy(
                fast_period=2,
                slow_period=4,
                rsi_period=3,
                bollinger_period=5,
                atr_period=3,
                volume_period=3,
                visual_lookback=5,
                min_volume_ratio=Decimal("0.5"),
            ),
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    interval="1h",
                    base_volume=Decimal("100"),
                    last_volume=Decimal("200"),
                )
            )
        )

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertEqual(signal.metadata["primary_strategy"], "bb_volume_reversion")
        self.assertIn("Bollinger reversion", signal.metadata["selection_reason"])

    def test_adaptive_hybrid_exits_open_long_before_reversing_short(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("101"),
            Decimal("102"),
            Decimal("103"),
            Decimal("104"),
            Decimal("103"),
            Decimal("95"),
        ]
        strategy = AdaptiveHybridStrategy(
            ema_strategy=EMARSICrossoverStrategy(
                fast_period=2,
                slow_period=4,
                rsi_period=3,
                atr_period=3,
                short_rsi_min=Decimal("0"),
            ),
            exit_on_opposite_entry=True,
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(closes),
                features={
                    "open_position": {
                        "pair": "B-BTC_USDT",
                        "direction": "long",
                        "entry_price": Decimal("100"),
                        "quantity": Decimal("1"),
                    }
                },
            )
        )

        self.assertEqual(signal.action, SignalAction.EXIT_LONG)
        self.assertEqual(signal.direction, SignalDirection.LONG)

    def test_adaptive_hybrid_uses_open_interest_to_block_weak_shorts(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("101"),
            Decimal("102"),
            Decimal("103"),
            Decimal("104"),
            Decimal("103"),
            Decimal("95"),
        ]
        strategy = AdaptiveHybridStrategy(
            ema_strategy=EMARSICrossoverStrategy(
                fast_period=2,
                slow_period=4,
                rsi_period=3,
                atr_period=3,
                short_rsi_min=Decimal("0"),
            ),
            visual_filter=HybridMetaStrategy(
                fast_period=2,
                slow_period=4,
                rsi_period=3,
                bollinger_period=5,
                atr_period=3,
                volume_period=3,
                visual_lookback=5,
                min_volume_ratio=Decimal("0.5"),
            ),
        )

        signal = strategy.evaluate(
            _context(
                _series_from_closes(
                    closes,
                    base_volume=Decimal("100"),
                    last_volume=Decimal("160"),
                ),
                features={
                    "open_interest": {
                        "source": "unit-test",
                        "score": "0.3",
                        "change_pct": "2",
                    }
                },
            )
        )

        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertIn("open interest", signal.reason)

    def test_bb_dynamic_grid_opens_long_at_lower_bollinger_boundary(self) -> None:
        strategy = BollingerDynamicFuturesGridStrategy(
            bollinger_period=5,
            atr_period=5,
        )
        series = _series_from_closes([Decimal("100")] * 20 + [Decimal("90")])

        signal = strategy.evaluate(_context(series))

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertEqual(signal.strategy_name, "bb_dynamic_grid")
        self.assertEqual(signal.metadata["grid_entries"], 1)
        self.assertEqual(signal.metadata["exit_mode"], "close_confirmed_dynamic_trail")
        self.assertIsNone(signal.take_profit)
        self.assertLess(signal.stop_loss, signal.entry_price)  # type: ignore[operator]

    def test_bb_dynamic_grid_scales_in_on_next_long_grid_level(self) -> None:
        strategy = BollingerDynamicFuturesGridStrategy(
            bollinger_period=5,
            atr_period=5,
            min_grid_spacing_pct=Decimal("0.01"),
        )
        series = _series_from_closes([Decimal("100")] * 25 + [Decimal("98")])

        signal = strategy.evaluate(
            _context(
                series,
                features={
                    "open_position": {
                        "pair": "B-BTC_USDT",
                        "direction": "long",
                        "entry_price": Decimal("100"),
                        "quantity": Decimal("1"),
                        "strategy_name": "bb_dynamic_grid",
                        "metadata": {
                            "grid_entries": 1,
                            "last_grid_entry_price": Decimal("100"),
                        },
                    }
                },
            )
        )

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertTrue(signal.metadata["allow_scale_in"])
        self.assertEqual(signal.metadata["grid_entries"], 2)

    def test_bb_dynamic_grid_exits_long_on_close_confirmed_trail_break(self) -> None:
        strategy = BollingerDynamicFuturesGridStrategy(
            bollinger_period=5,
            atr_period=5,
            trail_activation_pct=Decimal("1"),
            trail_distance_pct=Decimal("2"),
        )
        position = {
            "pair": "B-BTC_USDT",
            "direction": "long",
            "entry_price": Decimal("100"),
            "quantity": Decimal("1"),
            "strategy_name": "bb_dynamic_grid",
            "metadata": {"grid_entries": 1, "last_grid_entry_price": Decimal("100")},
        }

        first_signal = strategy.evaluate(
            _context(
                _series_from_closes([Decimal("100")] * 25 + [Decimal("105")]),
                features={"open_position": position},
            )
        )
        second_signal = strategy.evaluate(
            _context(
                _series_from_closes([Decimal("100")] * 25 + [Decimal("105"), Decimal("102")]),
                features={"open_position": position},
            )
        )

        self.assertEqual(first_signal.action, SignalAction.HOLD)
        self.assertEqual(second_signal.action, SignalAction.EXIT_LONG)
        self.assertEqual(second_signal.direction, SignalDirection.LONG)
        self.assertIn("dynamic futures-grid trailing line", second_signal.reason)


if __name__ == "__main__":
    unittest.main()
