from __future__ import annotations

import shutil
import tempfile
import unittest
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from app.config import Settings, RiskSettings
from app.data.candle_builder import OHLCVCandle, CandleSeries
from app.live.paper_loop import PaperTradingLoop, get_live_state
from app.persistence.paper_state import PaperSessionStore, PaperStateStore


def _candle(
    interval: str,
    open_time_ms: int,
    close: Decimal,
    is_closed: bool,
    pair: str = "B-BTC_USDT",
) -> OHLCVCandle:
    from app.data.candle_builder import interval_to_ms
    interval_ms = interval_to_ms(interval)
    return OHLCVCandle(
        pair=pair,
        interval=interval,
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + interval_ms - 1,
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=Decimal("100"),
        is_closed=is_closed
    )


class PaperClosedCandleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = Path(tempfile.mkdtemp())
        self.settings = Settings(
            paper_starting_equity=Decimal("10000"),
            risk=RiskSettings(),
            paper_intrabar_enabled=False
        )
        self.loop = PaperTradingLoop(
            self.settings,
            state_store=PaperStateStore(str(self.test_dir / "paper_state.db")),
            session_store=PaperSessionStore(str(self.test_dir / "paper_state.json")),
        )
        pair = "B-BTC_USDT"
        self.loop._watchlist = [pair]
        self.loop.series = {pair: CandleSeries()}
        self.loop.series[pair].add(_candle("1m", 0, Decimal("100"), True))
        
        # Mock dependencies
        self.loop.strategy_engine.evaluate = MagicMock(return_value=[])
        from app.data.gap_guard import CandleGapGuard
        self.loop.gap_guards = {pair: CandleGapGuard("1m")}

    def tearDown(self) -> None:
        self.loop.state_store.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_on_candle_ignores_partial_candle(self) -> None:
        # Initial count is 0
        self.loop.candle_count = 0
        pair = "B-BTC_USDT"
        
        # Partial candle (is_closed=False)
        partial = _candle("1m", 60000, Decimal("101"), False)
        self.loop._on_candle(partial)
        
        self.assertEqual(self.loop.candle_count, 0)
        self.assertEqual(len(self.loop.series[pair]), 1) # Only the warmup one

    def test_on_candle_accepts_closed_candle(self) -> None:
        self.loop.candle_count = 0
        pair = "B-BTC_USDT"
        
        # New closed series for this test method
        self.loop.series = {pair: CandleSeries()}
        self.loop.series[pair].add(_candle("1m", 0, Decimal("100"), True))

        # Closed candle (is_closed=True)
        closed = _candle("1m", 60000, Decimal("101"), True)
        self.loop._on_candle(closed)

        self.assertEqual(self.loop.candle_count, 1)
        self.assertEqual(len(self.loop.series[pair]), 2)

    def test_stream_snapshots_close_only_when_next_bucket_arrives(self) -> None:
        first_partial = _candle("1m", 60_000, Decimal("101"), True)
        first_later_update = _candle("1m", 60_000, Decimal("102"), True)
        next_partial = _candle("1m", 120_000, Decimal("103"), True)

        self.assertEqual(self.loop._closed_candles_from_stream(first_partial), [])
        self.assertEqual(self.loop._closed_candles_from_stream(first_later_update), [])

        closed = self.loop._closed_candles_from_stream(next_partial)

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].open_time_ms, 60_000)
        self.assertEqual(closed[0].close, Decimal("102"))
        self.assertTrue(closed[0].is_closed)

    def test_stream_pending_candles_are_isolated_by_pair(self) -> None:
        btc_first = _candle("1m", 60_000, Decimal("101"), True, pair="B-BTC_USDT")
        sol_first = _candle("1m", 60_000, Decimal("50"), True, pair="B-SOL_USDT")
        btc_next = _candle("1m", 120_000, Decimal("102"), True, pair="B-BTC_USDT")

        self.assertEqual(self.loop._closed_candles_from_stream(btc_first), [])
        self.assertEqual(self.loop._closed_candles_from_stream(sol_first), [])

        closed = self.loop._closed_candles_from_stream(btc_next)

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].pair, "B-BTC_USDT")
        self.assertEqual(self.loop._pending_stream_candles[("B-SOL_USDT", "1m")].close, Decimal("50"))

    def test_live_state_publishes_equity_and_candle_history(self) -> None:
        candle = _candle("1m", 60_000, Decimal("101"), True)
        self.loop.equity_history = [{"t": candle.close_time_ms, "equity": "10000"}]
        self.loop._record_live_candle(candle)

        self.loop._publish_live_snapshot(interval="1m", last_updated="test")
        state = get_live_state()
        candles_payload = json.loads(state["candles_json"])
        equity_history = json.loads(state["equity_history_json"])

        self.assertEqual(candles_payload["pair"], "B-BTC_USDT")
        self.assertEqual(candles_payload["candles"][-1]["close"], "101")
        self.assertIn("B-BTC_USDT", candles_payload["pairs"])
        self.assertEqual(
            candles_payload["pairs"]["B-BTC_USDT"]["candles"][-1]["close"],
            "101",
        )
        self.assertEqual(equity_history[-1]["equity"], "10000")

    def test_candle_payload_includes_every_running_watchlist_pair(self) -> None:
        btc_candle = _candle("1m", 60_000, Decimal("101"), True, pair="B-BTC_USDT")
        sol_candle = _candle("1m", 60_000, Decimal("55"), True, pair="B-SOL_USDT")
        self.loop._watchlist = ["B-BTC_USDT", "B-SOL_USDT"]
        self.loop._record_live_candle(btc_candle)
        self.loop._record_live_candle(sol_candle)

        payload = self.loop._candles_payload()

        self.assertIn("B-BTC_USDT", payload["pairs"])
        self.assertIn("B-SOL_USDT", payload["pairs"])
        self.assertEqual(payload["pairs"]["B-BTC_USDT"]["candles"][-1]["close"], "101")
        self.assertEqual(payload["pairs"]["B-SOL_USDT"]["candles"][-1]["close"], "55")


if __name__ == "__main__":
    unittest.main()
