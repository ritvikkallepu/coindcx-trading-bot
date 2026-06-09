from __future__ import annotations

import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.data.candle_builder import CandleSeries, OHLCVCandle
from app.live.live_loop import LiveTradingLoop
from app.strategies.base import SignalAction, SignalDirection, StrategySignal


PAIR = "B-BTC_USDT"


def _candle(
    *,
    interval: str = "5m",
    open_time_ms: int = 0,
    close_time_ms: int = 299_999,
    open_price: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
    volume: str = "100",
    is_closed: bool = True,
) -> OHLCVCandle:
    return OHLCVCandle(
        pair=PAIR,
        interval=interval,
        open_time_ms=open_time_ms,
        close_time_ms=close_time_ms,
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        is_closed=is_closed,
    )


def _parent_series() -> CandleSeries:
    series = CandleSeries(maxlen=100)
    for index in range(30):
        price = Decimal("100") + Decimal(index) / Decimal("10")
        series.add(
            _candle(
                interval="1h",
                open_time_ms=index * 3_600_000,
                close_time_ms=((index + 1) * 3_600_000) - 1,
                open_price=str(price - Decimal("0.1")),
                high=str(price + Decimal("0.2")),
                low=str(price - Decimal("0.2")),
                close=str(price),
            )
        )
    return series


def _momentum_signal(
    *,
    direction: SignalDirection = SignalDirection.LONG,
    final_score: str = "0.8",
    ema_score: str = "0.4",
    momentum: bool = True,
) -> StrategySignal:
    return StrategySignal(
        strategy_name="hybrid_meta_v2",
        pair=PAIR,
        interval="5m",
        action=(
            SignalAction.ENTER_LONG
            if direction == SignalDirection.LONG
            else SignalAction.ENTER_SHORT
        ),
        confidence=Decimal("0.8"),
        reason="Momentum ignition",
        timestamp_ms=1,
        direction=direction,
        entry_price=Decimal("105"),
        stop_loss=Decimal("95"),
        metadata={
            "momentum_ignition": momentum,
            "final_score": Decimal(final_score),
            "ema_score": Decimal(ema_score),
        },
    )


class LiveIntrabarMomentumTests(unittest.TestCase):
    def test_closed_execution_candle_is_evaluated_once(self) -> None:
        loop = object.__new__(LiveTradingLoop)
        loop.pairs = [PAIR]
        loop.interval = "1h"
        loop.execution_interval = "5m"
        loop.execution_series_by_pair = {PAIR: CandleSeries(maxlen=20)}
        loop._last_evaluated_strategy_candle_ms = {}
        loop._last_processed_candle_ms = {}
        loop._check_dry_run_stops = MagicMock()
        loop._apply_profit_protection = MagicMock()
        loop._process_execution_momentum_candle = MagicMock()
        loop.save_local_state = MagicMock()

        now_ms = int(time.time() * 1000)
        candle = _candle(
            open_time_ms=now_ms - 300_000,
            close_time_ms=now_ms - 1_000,
        )

        loop._on_candle(candle, source="test")
        loop._on_candle(candle, source="test")

        loop._process_execution_momentum_candle.assert_called_once_with(
            candle,
            source="test",
        )

    def test_forming_execution_candle_protects_profit_but_does_not_enter(self) -> None:
        loop = object.__new__(LiveTradingLoop)
        loop.pairs = [PAIR]
        loop.interval = "1h"
        loop.execution_interval = "5m"
        loop.execution_series_by_pair = {PAIR: CandleSeries(maxlen=20)}
        loop._last_evaluated_strategy_candle_ms = {}
        loop._last_processed_candle_ms = {}
        loop._check_dry_run_stops = MagicMock()
        loop._apply_profit_protection = MagicMock()
        loop._process_execution_momentum_candle = MagicMock()
        loop.save_local_state = MagicMock()

        candle = _candle(is_closed=False)
        loop._on_candle(candle, source="test")

        loop._apply_profit_protection.assert_called_once_with(candle)
        loop._process_execution_momentum_candle.assert_not_called()

    def test_execution_path_accepts_only_aligned_momentum_ignition(self) -> None:
        loop = object.__new__(LiveTradingLoop)
        loop.kill_switch_active = False
        loop.interval = "1h"
        loop.execution_interval = "5m"
        loop.strategy_engine = SimpleNamespace(
            strategies=[SimpleNamespace(name="hybrid_meta_v2")],
            evaluate=MagicMock(return_value=[_momentum_signal()]),
        )
        loop.series_by_pair = {PAIR: _parent_series()}
        loop.local_state = {"positions": {}}
        loop._pending_entry_pairs = set()
        loop._stopped_out_candles = {}
        loop._strategy_features = MagicMock(return_value={})
        loop._handle_signal = MagicMock()
        loop._add_diagnostic = MagicMock()

        candle = _candle()
        loop._process_execution_momentum_candle(candle, source="test")

        loop._handle_signal.assert_called_once()
        submitted = loop._handle_signal.call_args.args[0]
        self.assertTrue(submitted.metadata["live_execution_momentum_entry"])
        self.assertTrue(submitted.metadata["momentum_context_aligned"])
        self.assertEqual(submitted.metadata["context_interval"], "1h")

    def test_execution_path_rejects_misaligned_or_normal_entries(self) -> None:
        loop = object.__new__(LiveTradingLoop)
        loop.kill_switch_active = False
        loop.interval = "1h"
        loop.execution_interval = "5m"
        loop.strategy_engine = SimpleNamespace(
            strategies=[SimpleNamespace(name="hybrid_meta_v2")],
            evaluate=MagicMock(
                return_value=[
                    _momentum_signal(final_score="-0.8", ema_score="-0.4"),
                ]
            ),
        )
        loop.series_by_pair = {PAIR: _parent_series()}
        loop.local_state = {"positions": {}}
        loop._pending_entry_pairs = set()
        loop._stopped_out_candles = {}
        loop._strategy_features = MagicMock(return_value={})
        loop._handle_signal = MagicMock()
        loop._add_diagnostic = MagicMock()

        candle = _candle()
        loop._process_execution_momentum_candle(candle, source="test")
        loop._handle_signal.assert_not_called()
        loop._add_diagnostic.assert_called_once()

        loop.strategy_engine.evaluate.return_value = [_momentum_signal(momentum=False)]
        loop._add_diagnostic.reset_mock()
        loop._process_execution_momentum_candle(candle, source="test")
        loop._handle_signal.assert_not_called()
        loop._add_diagnostic.assert_not_called()

    def test_long_spike_high_activates_profit_floor_even_if_close_falls_back(self) -> None:
        loop = self._profit_protection_loop(active_pos="1", stop="90")

        with patch(
            "app.live.live_loop.latest_indicator_snapshot",
            return_value=SimpleNamespace(atr=Decimal("10")),
        ):
            loop._apply_profit_protection(
                _candle(high="130", low="104", close="105")
            )

        self.assertEqual(
            loop.execution_engine.update_tpsl.call_args.kwargs["stop_loss"],
            Decimal("104.9"),
        )
        self.assertEqual(
            loop.local_state["position_metadata"][PAIR]["max_r_hit"],
            "3",
        )
        self.assertTrue(
            loop.local_state["position_metadata"][PAIR][
                "profit_protection_crossed_market"
            ]
        )

    def test_short_spike_low_activates_profit_floor_even_if_close_falls_back(self) -> None:
        loop = self._profit_protection_loop(active_pos="-1", stop="110")

        with patch(
            "app.live.live_loop.latest_indicator_snapshot",
            return_value=SimpleNamespace(atr=Decimal("10")),
        ):
            loop._apply_profit_protection(
                _candle(high="96", low="70", close="95")
            )

        self.assertEqual(
            loop.execution_engine.update_tpsl.call_args.kwargs["stop_loss"],
            Decimal("95.1"),
        )
        self.assertEqual(
            loop.local_state["position_metadata"][PAIR]["max_r_hit"],
            "3",
        )

    @staticmethod
    def _profit_protection_loop(*, active_pos: str, stop: str) -> LiveTradingLoop:
        loop = object.__new__(LiveTradingLoop)
        loop.kill_switch_active = False
        loop.local_state = {
            "positions": {
                PAIR: {
                    "active_pos": active_pos,
                    "avg_price": "100",
                    "stop_loss_trigger": stop,
                    "source": "exchange",
                },
            },
            "position_metadata": {
                PAIR: {
                    "initial_entry_price": "100",
                    "initial_stop_loss": stop,
                    "initial_r": "10",
                    "max_r_hit": "0",
                },
            },
        }
        loop.series_by_pair = {PAIR: _parent_series()}
        loop.settings = SimpleNamespace(
            risk=SimpleNamespace(atr_trailing_multiple=Decimal("1.2"))
        )
        loop._instruments = {PAIR: SimpleNamespace(tick_size=Decimal("0.1"))}
        loop.execution_engine = SimpleNamespace(
            update_tpsl=MagicMock(return_value=SimpleNamespace(accepted=True))
        )
        loop.alert = MagicMock()
        loop.save_local_state = MagicMock()
        return loop


if __name__ == "__main__":
    unittest.main()
