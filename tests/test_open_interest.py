from __future__ import annotations

from io import BytesIO
import unittest
from decimal import Decimal
from urllib.error import HTTPError

from app.data.candle_builder import OHLCVCandle
from app.data.open_interest import (
    BINANCE_OI_MAX_HISTORY_MS,
    BinanceOpenInterestClient,
    OpenInterestPoint,
    binance_oi_request_window,
    binance_oi_period_for_interval,
    build_open_interest_features,
    coindcx_pair_to_binance_symbol,
    open_interest_confirmation_score,
)


def _candle(index: int, close: Decimal) -> OHLCVCandle:
    open_time_ms = index * 3_600_000
    return OHLCVCandle(
        pair="B-BTC_USDT",
        interval="1h",
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + 3_599_999,
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=Decimal("100"),
    )


class OpenInterestTests(unittest.TestCase):
    def test_maps_coindcx_futures_pair_to_binance_symbol(self) -> None:
        self.assertEqual(coindcx_pair_to_binance_symbol("B-BTC_USDT"), "BTCUSDT")
        self.assertEqual(coindcx_pair_to_binance_symbol("B-SOL_USDT"), "SOLUSDT")
        self.assertIsNone(coindcx_pair_to_binance_symbol("B-BTC_INR"))

    def test_uses_supported_binance_period_for_short_intervals(self) -> None:
        self.assertEqual(binance_oi_period_for_interval("1m"), "5m")
        self.assertEqual(binance_oi_period_for_interval("30min"), "30m")
        self.assertEqual(binance_oi_period_for_interval("24h"), "1d")

    def test_clamps_binance_oi_window_to_recent_history(self) -> None:
        now_ms = 2_000_000_000_000
        start_ms = now_ms - BINANCE_OI_MAX_HISTORY_MS - 3_600_000
        end_ms = now_ms

        window = binance_oi_request_window(
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            now_ms=now_ms,
        )

        self.assertEqual(window, (now_ms - BINANCE_OI_MAX_HISTORY_MS, end_ms))

    def test_returns_no_window_when_candles_are_older_than_binance_oi_history(self) -> None:
        now_ms = 2_000_000_000_000
        old_end_ms = now_ms - BINANCE_OI_MAX_HISTORY_MS - 1

        window = binance_oi_request_window(
            start_time_ms=old_end_ms - 3_600_000,
            end_time_ms=old_end_ms,
            now_ms=now_ms,
        )

        self.assertIsNone(window)

    def test_invalid_binance_proxy_symbol_returns_empty_points(self) -> None:
        def opener(*_args: object, **_kwargs: object) -> object:
            body = BytesIO(b'{"code":-1121,"msg":"Invalid symbol."}')
            raise HTTPError("https://fapi.binance.com", 400, "Bad Request", None, body)

        client = BinanceOpenInterestClient(opener=opener)

        points = client.get_open_interest_history(
            symbol="BSBUSDT",
            period="1h",
            start_time_ms=1,
            end_time_ms=2,
        )

        self.assertEqual(points, [])

    def test_confirmation_score_marks_rising_oi_with_rising_price_bullish(self) -> None:
        score = open_interest_confirmation_score(
            price_change_pct=Decimal("1.5"),
            open_interest_change_pct=Decimal("4"),
        )

        self.assertGreater(score, Decimal("0"))

    def test_confirmation_score_marks_rising_oi_with_falling_price_bearish(self) -> None:
        score = open_interest_confirmation_score(
            price_change_pct=Decimal("-1.5"),
            open_interest_change_pct=Decimal("4"),
        )

        self.assertLess(score, Decimal("0"))

    def test_build_features_uses_latest_historical_oi_without_lookahead(self) -> None:
        candles = [
            _candle(0, Decimal("100")),
            _candle(1, Decimal("102")),
            _candle(2, Decimal("101")),
        ]
        points = [
            OpenInterestPoint(
                timestamp_ms=candles[0].close_time_ms,
                symbol="BTCUSDT",
                open_interest=Decimal("1000"),
            ),
            OpenInterestPoint(
                timestamp_ms=candles[1].close_time_ms,
                symbol="BTCUSDT",
                open_interest=Decimal("1100"),
            ),
            OpenInterestPoint(
                timestamp_ms=candles[2].close_time_ms + 10_000,
                symbol="BTCUSDT",
                open_interest=Decimal("1500"),
            ),
        ]

        features = build_open_interest_features(
            pair="B-BTC_USDT",
            interval="1h",
            candles=candles,
            points=points,
        )

        first_feature = features.latest_at_or_before(candles[1].close_time_ms)
        second_feature = features.latest_at_or_before(candles[2].close_time_ms)
        self.assertIsNotNone(first_feature)
        assert first_feature is not None
        self.assertEqual(first_feature["source"], "binance_proxy")
        self.assertEqual(first_feature["symbol"], "BTCUSDT")
        self.assertGreater(first_feature["score"], Decimal("0"))
        self.assertEqual(second_feature, first_feature)


if __name__ == "__main__":
    unittest.main()
