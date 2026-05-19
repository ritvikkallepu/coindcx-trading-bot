from __future__ import annotations

import unittest

from app.exchange.coindcx_channels import (
    futures_candle_channel,
    futures_current_prices_channel,
    futures_orderbook_channel,
    futures_price_channel,
    futures_trade_channel,
)


class ChannelTests(unittest.TestCase):
    def test_futures_channels_match_coindcx_docs(self) -> None:
        self.assertEqual(futures_trade_channel("B-BTC_USDT"), "B-BTC_USDT@trades-futures")
        self.assertEqual(futures_price_channel("B-BTC_USDT"), "B-BTC_USDT@prices-futures")
        self.assertEqual(futures_candle_channel("B-BTC_USDT", "1m"), "B-BTC_USDT_1m-futures")
        self.assertEqual(
            futures_orderbook_channel("B-BTC_USDT", 50),
            "B-BTC_USDT@orderbook@50-futures",
        )
        self.assertEqual(futures_current_prices_channel(), "currentPrices@futures@rt")

    def test_invalid_orderbook_depth_rejected(self) -> None:
        with self.assertRaises(ValueError):
            futures_orderbook_channel("B-BTC_USDT", 25)

    def test_invalid_candle_interval_rejected(self) -> None:
        with self.assertRaises(ValueError):
            futures_candle_channel("B-BTC_USDT", "2m")


if __name__ == "__main__":
    unittest.main()

