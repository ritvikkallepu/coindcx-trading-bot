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
from app.strategies.rsi_macd_momentum import RSIMACDMomentumStrategy


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
    def test_all_strategy_choice_includes_all_registered_strategies(self) -> None:
        engine = strategy_engine_for_name("all")
        names = {strategy.name for strategy in engine.strategies}

        self.assertIn("ema_rsi_trend", names)
        self.assertIn("bb_volume_reversion", names)
        self.assertIn("hybrid_meta", names)
        self.assertIn("hybrid_meta_v2", names)
        self.assertIn("rsi_macd_momentum", names)
        self.assertIn("adaptive_hybrid", names)
        self.assertIn("bb_dynamic_grid", names)

    def test_rsi_macd_strategy_is_selectable(self) -> None:
        engine = strategy_engine_for_name("rsi_macd_momentum")

        self.assertEqual(engine.strategies[0].name, "rsi_macd_momentum")

    def test_rsi_macd_strategy_enters_long_and_disables_external_exits(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("99"),
            Decimal("98"),
            Decimal("97"),
            Decimal("96"),
            Decimal("95"),
            Decimal("94"),
            Decimal("95"),
            Decimal("96"),
            Decimal("98"),
            Decimal("101"),
            Decimal("104"),
        ]
        strategy = RSIMACDMomentumStrategy(
            rsi_period=3,
            macd_fast_period=2,
            macd_slow_period=5,
            macd_signal_period=2,
            atr_period=3,
            volume_period=3,
            extension_lookback=3,
            exhaustion_lookback=4,
            max_extension_atr=Decimal("3"),
            min_macd_histogram_atr=Decimal("0.001"),
            rsi_long_max=Decimal("100"),
            macd_cross_max_age=10,
            rsi_cross_max_age=10,
        )

        signal = strategy.evaluate(_context(_series_from_closes(closes)))

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertEqual(signal.direction, SignalDirection.LONG)
        self.assertIsNone(signal.take_profit)
        self.assertFalse(signal.metadata["atr_dynamic_exits_enabled"])
        self.assertFalse(signal.metadata["bb_trail_enabled"])
        self.assertTrue(signal.metadata["strategy_managed_exits"])

    def test_rsi_macd_strategy_enters_short(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("101"),
            Decimal("102"),
            Decimal("103"),
            Decimal("104"),
            Decimal("105"),
            Decimal("106"),
            Decimal("105"),
            Decimal("104"),
            Decimal("102"),
            Decimal("99"),
            Decimal("96"),
        ]
        strategy = RSIMACDMomentumStrategy(
            rsi_period=3,
            macd_fast_period=2,
            macd_slow_period=5,
            macd_signal_period=2,
            atr_period=3,
            volume_period=3,
            extension_lookback=3,
            exhaustion_lookback=4,
            max_extension_atr=Decimal("3"),
            min_macd_histogram_atr=Decimal("0.001"),
            rsi_short_min=Decimal("0"),
            macd_cross_max_age=10,
            rsi_cross_max_age=10,
        )

        signal = strategy.evaluate(_context(_series_from_closes(closes)))

        self.assertEqual(signal.action, SignalAction.ENTER_SHORT)
        self.assertEqual(signal.direction, SignalDirection.SHORT)

    def test_rsi_macd_strategy_exits_long_on_confirmed_momentum_loss(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("99"),
            Decimal("98"),
            Decimal("97"),
            Decimal("96"),
            Decimal("95"),
            Decimal("94"),
            Decimal("95"),
            Decimal("96"),
            Decimal("98"),
            Decimal("101"),
            Decimal("104"),
            Decimal("102"),
            Decimal("99"),
            Decimal("96"),
        ]
        series = _series_from_closes(closes)
        open_position = {
            "pair": "B-BTC_USDT",
            "direction": "long",
            "entry_price": Decimal("104"),
            "opened_at_ms": series[-4].close_time_ms,
            "strategy_name": "rsi_macd_momentum",
            "metadata": {"strategy_exit_model": "rsi_macd_momentum"},
        }
        strategy = RSIMACDMomentumStrategy(
            rsi_period=3,
            macd_fast_period=2,
            macd_slow_period=5,
            macd_signal_period=2,
            atr_period=3,
            volume_period=3,
            extension_lookback=3,
            exhaustion_lookback=4,
            exit_histogram_confirm_candles=1,
            min_macd_histogram_atr=Decimal("0.001"),
        )

        signal = strategy.evaluate(_context(series, features={"open_position": open_position}))

        self.assertEqual(signal.action, SignalAction.EXIT_LONG)
        self.assertEqual(signal.metadata["exit_trigger_type"], "strategy_momentum_exit")

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
        closes = [Decimal("100")] * 60 + [
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

    def test_hybrid_meta_profile_risk_multiplier_reduces_default_entry_risk(self) -> None:
        series = _series_from_closes([Decimal("100")] * 30)
        strategy = HybridMetaStrategy()

        signal = strategy._entry_signal(
            context=_context(
                series,
                features={
                    "backtest_config": {
                        "profile_risk_multiplier": Decimal("0.75"),
                    },
                },
            ),
            direction=SignalDirection.LONG,
            final_score=Decimal("0.40"),
            atr=Decimal("1"),
            reason="unit profile entry",
            metadata={
                "final_score": Decimal("0.40"),
                "ema_score": Decimal("0.20"),
                "bb_score": Decimal("0.10"),
                "visual_score": Decimal("0.10"),
                "open_interest_score": Decimal("0"),
                "visual": {"volume_ratio": Decimal("1")},
            },
        )

        self.assertEqual(signal.metadata["risk_multiplier"], Decimal("0.75"))
        self.assertTrue(signal.metadata["risk_multiplier_applies"])
        self.assertEqual(signal.metadata["profile_risk_multiplier"], Decimal("0.75"))

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
        strategy = HybridMetaStrategy()
        
        # When OI is inactive, active_weight should be sum of EMA, BB, Visual (0.45 + 0.25 + 0.20 = 0.90)
        # We use scores that do not conflict to avoid BB damping (both positive)
        _, active_weight_no_oi = strategy._combined_score(
            ema_score=Decimal("0.5"),
            bb_score=Decimal("0.5"),
            visual_score=Decimal("0.5"),
            oi_score=Decimal("0"),
            oi_active=False,
        )
        self.assertEqual(active_weight_no_oi, Decimal("0.90"))

        # When OI is active, active_weight should include OI weight (0.90 + 0.10 = 1.00)
        _, active_weight_with_oi = strategy._combined_score(
            ema_score=Decimal("0.5"),
            bb_score=Decimal("0.5"),
            visual_score=Decimal("0.5"),
            oi_score=Decimal("0.5"),
            oi_active=True,
        )
        self.assertEqual(active_weight_with_oi, Decimal("1.00"))

    def test_hybrid_meta_v2_excludes_bollinger_from_hybrid_score(self) -> None:
        strategy = HybridMetaV2Strategy()

        bearish_bb_score, active_weight = strategy._combined_score(
            ema_score=Decimal("0.6"),
            bb_score=Decimal("-1.0"),
            visual_score=Decimal("0.6"),
            oi_score=Decimal("0"),
            oi_active=False,
            include_bb=strategy.hybrid_bollinger_score_enabled,
        )
        bullish_bb_score, _ = strategy._combined_score(
            ema_score=Decimal("0.6"),
            bb_score=Decimal("1.0"),
            visual_score=Decimal("0.6"),
            oi_score=Decimal("0"),
            oi_active=False,
            include_bb=strategy.hybrid_bollinger_score_enabled,
        )

        self.assertFalse(strategy.hybrid_bollinger_score_enabled)
        self.assertEqual(active_weight, Decimal("0.70"))
        self.assertEqual(bearish_bb_score, bullish_bb_score)

    def test_hybrid_meta_requires_open_interest_for_new_short(self) -> None:
        closes = [Decimal("100")] * 60 + [
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
            allow_short_without_open_interest=False,
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

    def test_hybrid_meta_v2_allows_reduced_risk_intrabar_reversal_breakout(self) -> None:
        parent_series = _series_from_closes(
            [Decimal("100")] * 60,
            interval="5m",
            base_volume=Decimal("100"),
        )
        execution_candles = [
            OHLCVCandle(
                pair="B-BTC_USDT",
                interval="1m",
                open_time_ms=1_800_000 + index * 60_000,
                close_time_ms=1_800_000 + ((index + 1) * 60_000) - 1,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
            for index, (open_price, high, low, close, volume) in enumerate(
                [
                    (
                        Decimal("99.8"),
                        Decimal("100.0"),
                        Decimal("99.7"),
                        Decimal("99.9"),
                        Decimal("100"),
                    ),
                    (
                        Decimal("99.9"),
                        Decimal("100.1"),
                        Decimal("99.8"),
                        Decimal("100.0"),
                        Decimal("100"),
                    ),
                    (
                        Decimal("100.0"),
                        Decimal("101.3"),
                        Decimal("99.9"),
                        Decimal("101.2"),
                        Decimal("250"),
                    ),
                ]
            )
        ]

        signal = HybridMetaV2Strategy().evaluate(
            _context(
                parent_series,
                features={
                    "execution_candles": execution_candles,
                    "backtest_config": {
                        "intrabar_reversal_breakout_enabled": True,
                    },
                },
            )
        )

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertEqual(signal.interval, "1m")
        self.assertEqual(signal.entry_price, Decimal("101.2"))
        self.assertEqual(signal.metadata["entry_type"], "intrabar_reversal_breakout")
        self.assertEqual(signal.metadata["risk_multiplier"], Decimal("0.50"))
        self.assertEqual(signal.metadata["atr_profile"], "breakout_runner")
        self.assertFalse(signal.metadata["atr_take_profit_enabled"])
        self.assertTrue(signal.metadata["profit_lock_enabled"])

    def test_hybrid_meta_v2_default_entries_include_adaptive_stop_management(self) -> None:
        closes = [
            Decimal("100"),
            Decimal("100.5"),
            Decimal("101"),
            Decimal("101.5"),
            Decimal("102"),
            Decimal("103"),
            Decimal("104"),
            Decimal("105"),
            Decimal("106"),
            Decimal("107"),
            Decimal("108"),
            Decimal("109"),
            Decimal("110"),
            Decimal("111"),
            Decimal("112"),
            Decimal("113"),
            Decimal("114"),
            Decimal("115"),
            Decimal("116"),
            Decimal("117"),
            Decimal("118"),
            Decimal("119"),
            Decimal("120"),
            Decimal("121"),
            Decimal("122"),
            Decimal("123"),
            Decimal("124"),
            Decimal("125"),
            Decimal("126"),
            Decimal("128"),
        ]

        signal = HybridMetaV2Strategy().evaluate(
            _context(
                _series_from_closes(
                    closes,
                    base_volume=Decimal("100"),
                    last_volume=Decimal("180"),
                )
            )
        )

        if signal.action == SignalAction.ENTER_LONG:
            self.assertTrue(signal.metadata["atr_dynamic_exits_enabled"])
            self.assertTrue(signal.metadata["atr_trailing_enabled"])
            self.assertTrue(signal.metadata["breakeven_enabled"])
            self.assertTrue(signal.metadata["profit_lock_enabled"])
            self.assertTrue(signal.metadata["adaptive_stop_management_enabled"])
            self.assertNotIn("breakeven_activation_r", signal.metadata)
            self.assertNotIn("profit_lock_activation_r", signal.metadata)

    def test_hybrid_meta_v2_allows_reduced_risk_momentum_ignition(self) -> None:
        parent_series = _series_from_closes(
            [Decimal("100")] * 60,
            interval="5m",
            base_volume=Decimal("100"),
        )
        execution_candles = [
            OHLCVCandle(
                pair="B-BTC_USDT",
                interval="1m",
                open_time_ms=1_800_000,
                close_time_ms=1_859_999,
                open=Decimal("99.8"),
                high=Decimal("100.0"),
                low=Decimal("99.7"),
                close=Decimal("99.9"),
                volume=Decimal("100"),
            ),
            OHLCVCandle(
                pair="B-BTC_USDT",
                interval="1m",
                open_time_ms=1_860_000,
                close_time_ms=1_919_999,
                open=Decimal("101"),
                high=Decimal("105.2"),
                low=Decimal("100.8"),
                close=Decimal("105"),
                volume=Decimal("350"),
            ),
        ]

        signal = HybridMetaV2Strategy().evaluate(
            _context(
                parent_series,
                features={
                    "execution_candles": execution_candles,
                    "backtest_config": {
                        "intrabar_reversal_breakout_enabled": True,
                        "previous_parent_high": Decimal("100"),
                        "reversal_breakout_ignition_volume_ratio": Decimal("3.0"),
                        "reversal_breakout_ignition_body_ratio": Decimal("0.70"),
                        "reversal_breakout_ignition_close_position_ratio": Decimal("0.75"),
                        "reversal_breakout_ignition_max_extension_atr": Decimal("5.0"),
                    },
                },
            )
        )

        self.assertEqual(signal.action, SignalAction.ENTER_LONG)
        self.assertEqual(signal.entry_price, Decimal("105"))
        self.assertEqual(signal.metadata["breakout_variant"], "momentum_ignition")
        self.assertTrue(signal.metadata["momentum_ignition"])
        self.assertEqual(signal.metadata["risk_multiplier"], Decimal("0.25"))
        self.assertEqual(signal.metadata["atr_profile"], "momentum_ignition_runner")
        self.assertEqual(signal.metadata["atr_take_profit_mode"], "none")

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
