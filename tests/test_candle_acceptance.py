from __future__ import annotations

import unittest
import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from app.config import Settings
from app.live.live_loop import (
    LiveTradingLoop,
    _format_interval_boundary,
    _next_interval_boundary_ms,
)
from app.data.candle_builder import OHLCVCandle

class TestCandleAcceptance(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            live_pilot_dry_run=True,
            live_trading_enabled=False,
            live_closed_candle_buffer_ms=2000,
            strategy_interval="5m"
        )
        self.loop = LiveTradingLoop(self.settings, pairs=["B-BTC_USDT"], interval="5m")
        self.loop.strategy_engine = MagicMock()
        self.loop.strategy_engine.evaluate.return_value = []
        # Ensure warmup doesn't block
        self.loop._is_warming_up["B-BTC_USDT"] = False
        self.loop.kill_switch_active = False
        self.loop.local_state["kill_switch_active"] = False
        
        # Explicitly set logger level for the test
        import logging
        logging.getLogger("app.live.live_loop").setLevel(logging.DEBUG)

    @patch("app.live.live_loop.logger")
    def test_future_closed_candle_is_skipped(self, mock_logger):
        # Align now to a 5m boundary
        interval_ms = 300000
        base_time_ms = (int(time.time() * 1000) // interval_ms) * interval_ms
        
        # Current time: base_time_ms
        future_close_ms = base_time_ms + interval_ms
        
        candle = OHLCVCandle(
            pair="B-BTC_USDT",
            interval="5m",
            open_time_ms=base_time_ms,
            close_time_ms=future_close_ms - 1,
            open=Decimal("50000"),
            high=Decimal("51000"),
            low=Decimal("49000"),
            close=Decimal("50500"),
            volume=Decimal("100"),
            is_closed=True
        )

        with patch("time.time", return_value=base_time_ms / 1000.0):
            self.loop._on_candle(candle, source="websocket")
            
        # Ensure it was NOT counted as a closed candle
        self.assertEqual(self.loop.candle_count, 0)
        # Ensure evaluate was NOT called
        self.loop.strategy_engine.evaluate.assert_not_called()

    def test_strategy_candle_waits_for_post_close_buffer(self):
        interval_ms = 300000
        base_time_ms = (int(time.time() * 1000) // interval_ms) * interval_ms
        close_time_ms = base_time_ms + interval_ms - 1

        for index in range(25):
            open_time_ms = base_time_ms - ((25 - index) * interval_ms)
            self.loop.series_by_pair["B-BTC_USDT"].add(
                OHLCVCandle(
                    pair="B-BTC_USDT",
                    interval="5m",
                    open_time_ms=open_time_ms,
                    close_time_ms=open_time_ms + interval_ms - 1,
                    open=Decimal("50000"),
                    high=Decimal("50000"),
                    low=Decimal("50000"),
                    close=Decimal("50000"),
                    volume=Decimal("1"),
                    is_closed=True,
                )
            )

        candle = OHLCVCandle(
            pair="B-BTC_USDT",
            interval="5m",
            open_time_ms=base_time_ms,
            close_time_ms=close_time_ms,
            open=Decimal("50000"),
            high=Decimal("51000"),
            low=Decimal("49000"),
            close=Decimal("50500"),
            volume=Decimal("100"),
            is_closed=True,
        )

        with patch("time.time", return_value=(close_time_ms - 1000) / 1000.0):
            self.loop._on_candle(candle, source="websocket")

        self.loop.strategy_engine.evaluate.assert_not_called()
        self.assertEqual(self.loop.candle_count, 0)

        ready_time_ms = close_time_ms + self.settings.live_closed_candle_buffer_ms + 1
        with patch("time.time", return_value=ready_time_ms / 1000.0):
            self.loop._flush_matured_candles()

        self.loop.strategy_engine.evaluate.assert_called_once()
        self.assertEqual(self.loop.candle_count, 1)

    def test_execution_entry_waits_for_post_close_buffer(self):
        interval_ms = 60000
        base_time_ms = (int(time.time() * 1000) // interval_ms) * interval_ms
        close_time_ms = base_time_ms + interval_ms - 1
        candle = OHLCVCandle(
            pair="B-BTC_USDT",
            interval="1m",
            open_time_ms=base_time_ms,
            close_time_ms=close_time_ms,
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("99"),
            close=Decimal("104"),
            volume=Decimal("100"),
            is_closed=True,
        )
        self.loop._process_execution_momentum_candle = MagicMock()

        with patch("time.time", return_value=(close_time_ms - 500) / 1000.0):
            self.loop._on_candle(candle, source="websocket")

        self.loop._process_execution_momentum_candle.assert_not_called()

        ready_time_ms = close_time_ms + self.settings.live_closed_candle_buffer_ms + 1
        with patch("time.time", return_value=ready_time_ms / 1000.0):
            self.loop._flush_matured_candles()

        self.loop._process_execution_momentum_candle.assert_called_once()

    @patch("app.live.live_loop.logger")
    def test_forming_strategy_candle_logs_scheduled_close_once(self, mock_logger):
        interval_ms = 300000
        base_time_ms = (int(time.time() * 1000) // interval_ms) * interval_ms
        candle = OHLCVCandle(
            pair="B-BTC_USDT",
            interval="5m",
            open_time_ms=base_time_ms,
            close_time_ms=base_time_ms + interval_ms - 1,
            open=Decimal("50000"),
            high=Decimal("51000"),
            low=Decimal("49000"),
            close=Decimal("50500"),
            volume=Decimal("100"),
            is_closed=False,
        )

        with patch("time.time", return_value=base_time_ms / 1000.0):
            self.loop._on_candle(candle, source="websocket")
            self.loop._on_candle(candle, source="websocket")

        forming_logs = [
            call
            for call in mock_logger.info.call_args_list
            if call.args and "Forming strategy candle observed" in call.args[0]
        ]
        self.assertEqual(len(forming_logs), 1)

    def test_next_hour_boundary_is_utc_aligned_and_explicit(self):
        now_utc = datetime(2026, 6, 3, 23, 14, tzinfo=timezone.utc)
        now_ms = int(now_utc.timestamp() * 1000)

        boundary_ms = _next_interval_boundary_ms(now_ms, "1h")
        boundary_utc = datetime.fromtimestamp(boundary_ms / 1000, timezone.utc)

        self.assertEqual(boundary_utc, datetime(2026, 6, 4, 0, 0, tzinfo=timezone.utc))
        self.assertIn("2026-06-04 00:00:00 UTC", _format_interval_boundary(boundary_ms))

    @patch("app.live.live_loop.logger")
    def test_accepted_closed_candle_logs_properly(self, mock_logger):
        interval_ms = 300000
        base_time_ms = (int(time.time() * 1000) // interval_ms) * interval_ms
        now_ms = base_time_ms + 10000
        past_close_ms = base_time_ms
        past_open_ms = past_close_ms - interval_ms
        
        candle = OHLCVCandle(
            pair="B-BTC_USDT",
            interval="5m",
            open_time_ms=past_open_ms,
            close_time_ms=past_close_ms - 1,
            open=Decimal("50000"),
            high=Decimal("51000"),
            low=Decimal("49000"),
            close=Decimal("50500"),
            volume=Decimal("100"),
            is_closed=True
        )

        self.loop.strategy_engine = MagicMock()
        self.loop.strategy_engine.evaluate.return_value = []

        for i in range(25):
            c = OHLCVCandle(
                pair="B-BTC_USDT", interval="5m",
                open_time_ms=past_open_ms - (26-i)*interval_ms,
                close_time_ms=past_open_ms - (25-i)*interval_ms - 1,
                open=Decimal("50000"), high=Decimal("50000"), low=Decimal("50000"), close=Decimal("50000"),
                volume=Decimal("1"), is_closed=True
            )
            self.loop.series_by_pair["B-BTC_USDT"].add(c)
            self.loop._last_processed_candle_ms["B-BTC_USDT"] = c.close_time_ms

        with patch("time.time", return_value=now_ms / 1000.0):
            self.loop._on_candle(candle, source="websocket")
            
        # Verify logs
        found_eval = any("Strategy evaluated closed candle" in (call.args[0] % call.args[1:] if len(call.args) > 1 else call.args[0])
                         for call in mock_logger.info.call_args_list)
        
        self.assertTrue(found_eval, f"Eval log not found in {mock_logger.info.call_args_list}")
        self.assertEqual(self.loop.candle_count, 1)

    @patch("app.live.live_loop.logger")
    def test_strategy_evaluation_skip_reason_logged(self, mock_logger):
        interval_ms = 300000
        base_time_ms = (int(time.time() * 1000) // interval_ms) * interval_ms
        now_ms = base_time_ms + 10000
        
        self.loop.kill_switch_active = True
        
        candle = OHLCVCandle(
            pair="B-BTC_USDT",
            interval="5m",
            open_time_ms=base_time_ms - interval_ms,
            close_time_ms=base_time_ms - 1,
            open=Decimal("50000"),
            high=Decimal("51000"),
            low=Decimal("49000"),
            close=Decimal("50500"),
            volume=Decimal("100"),
            is_closed=True
        )

        with patch("time.time", return_value=now_ms / 1000.0):
            self.loop._on_candle(candle, source="websocket")
            
        found_skip = any("Strategy evaluation skipped: KILL SWITCH ACTIVE" in (call.args[0] % call.args[1:] if len(call.args) > 1 else call.args[0])
                         for call in mock_logger.info.call_args_list)
        self.assertTrue(found_skip, f"Skip reason not found in {mock_logger.info.call_args_list}")

if __name__ == "__main__":
    unittest.main()
