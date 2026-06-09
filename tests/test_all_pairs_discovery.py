from __future__ import annotations

import unittest
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch, ANY
from app.config import Settings, RiskSettings
from app.live.live_loop import LiveTradingLoop

class TestAllPairsDiscovery(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            live_pilot_dry_run=True,
            live_trading_enabled=False,
            trading_mode="paper"
        )
        
    @patch("urllib.request.urlopen")
    @patch("app.exchange.coindcx_rest.CoinDCXFuturesClient.get_active_instruments")
    def test_all_pairs_allowed_in_dry_run(self, mock_get_active, mock_urlopen):
        mock_get_active.return_value = ["B-BTC_USDT", "B-ETH_USDT", "B-XRP_USDT"]
        
        ticker_data = [
            {"market": "BTCUSDT", "volume": "100", "last_price": "70000", "bid": "69990", "ask": "70010"},
            {"market": "ETHUSDT", "volume": "1000", "last_price": "3500", "bid": "3499", "ask": "3501"},
            {"market": "XRPUSDT", "volume": "200000000", "last_price": "0.60", "bid": "0.5995", "ask": "0.6005"},
        ]
        
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(ticker_data).encode()
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response
        
        loop = LiveTradingLoop(self.settings, pairs=["ALL"])
        
        self.assertIn("B-BTC_USDT", loop.pairs)
        self.assertIn("B-ETH_USDT", loop.pairs)
        self.assertIn("B-XRP_USDT", loop.pairs)

    def test_all_pairs_rejected_in_real_live(self):
        settings = Settings(
            trading_mode="live",
            live_trading_enabled=True,
            live_pilot_dry_run=False,
            live_confirm_i_understand_risk="YES"
        )
        with self.assertRaises(ValueError) as cm:
            LiveTradingLoop(settings, pairs=["ALL"])
        self.assertIn("not allowed for real live trading", str(cm.exception))

    @patch("urllib.request.urlopen")
    @patch("app.exchange.coindcx_rest.CoinDCXFuturesClient.get_active_instruments")
    def test_blocked_pairs_excluded(self, mock_get_active, mock_urlopen):
        self.settings = replace_blocked_pairs(self.settings, ["B-ETH_USDT"])
        mock_get_active.return_value = ["B-BTC_USDT", "B-ETH_USDT"]
        
        ticker_data = [
            {"market": "BTCUSDT", "volume": "100", "last_price": "70000", "bid": "69990", "ask": "70010"},
            {"market": "ETHUSDT", "volume": "1000", "last_price": "3500", "bid": "3499", "ask": "3501"},
        ]
        
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(ticker_data).encode()
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response
        
        loop = LiveTradingLoop(self.settings, pairs=["ALL"])
        self.assertIn("B-BTC_USDT", loop.pairs)
        self.assertNotIn("B-ETH_USDT", loop.pairs)

    @patch("urllib.request.urlopen")
    @patch("app.exchange.coindcx_rest.CoinDCXFuturesClient.get_active_instruments")
    def test_low_volume_pairs_excluded(self, mock_get_active, mock_urlopen):
        # Default min volume is 1M USDT
        mock_get_active.return_value = ["B-BTC_USDT", "B-LOWVOL_USDT"]
        
        ticker_data = [
            {"market": "BTCUSDT", "volume": "100", "last_price": "70000", "bid": "69990", "ask": "70010"}, # 7M vol
            {"market": "LOWVOLUSDT", "volume": "100", "last_price": "10", "bid": "9", "ask": "11"}, # 1k vol
        ]
        
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(ticker_data).encode()
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response
        
        loop = LiveTradingLoop(self.settings, pairs=["ALL"])
        self.assertIn("B-BTC_USDT", loop.pairs)
        self.assertNotIn("B-LOWVOL_USDT", loop.pairs)

    @patch("urllib.request.urlopen")
    @patch("app.exchange.coindcx_rest.CoinDCXFuturesClient.get_active_instruments")
    def test_high_spread_pairs_excluded(self, mock_get_active, mock_urlopen):
        # Default max spread is 0.30%
        mock_get_active.return_value = ["B-BTC_USDT", "B-HIGHSPREAD_USDT"]
        
        ticker_data = [
            {"market": "BTCUSDT", "volume": "100", "last_price": "70000", "bid": "69990", "ask": "70010"}, # small spread
            {"market": "HIGHSPREADUSDT", "volume": "1000000", "last_price": "1", "bid": "1.0", "ask": "1.1"}, # 10% spread
        ]
        
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(ticker_data).encode()
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response
        
        loop = LiveTradingLoop(self.settings, pairs=["ALL"])
        self.assertIn("B-BTC_USDT", loop.pairs)
        self.assertNotIn("B-HIGHSPREAD_USDT", loop.pairs)

    @patch("urllib.request.urlopen")
    @patch("app.exchange.coindcx_rest.CoinDCXFuturesClient.get_active_instruments")
    def test_max_auto_pairs_cap_enforced(self, mock_get_active, mock_urlopen):
        from dataclasses import replace
        risk = replace(self.settings.risk, live_max_auto_pairs=2)
        self.settings = replace(self.settings, risk=risk)
        
        mock_get_active.return_value = ["B-P1_USDT", "B-P2_USDT", "B-P3_USDT"]
        
        ticker_data = [
            {"market": "P1USDT", "volume": "3000000", "last_price": "1", "bid": "0.999", "ask": "1.001"},
            {"market": "P2USDT", "volume": "2000000", "last_price": "1", "bid": "0.999", "ask": "1.001"},
            {"market": "P3USDT", "volume": "1000000", "last_price": "1", "bid": "0.999", "ask": "1.001"},
        ]
        
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(ticker_data).encode()
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response
        
        loop = LiveTradingLoop(self.settings, pairs=["ALL"])
        self.assertEqual(len(loop.pairs), 2)
        self.assertIn("B-P1_USDT", loop.pairs)
        self.assertIn("B-P2_USDT", loop.pairs)
        self.assertNotIn("B-P3_USDT", loop.pairs)

def replace_blocked_pairs(settings, blocked):
    from dataclasses import replace
    return replace(settings, live_blocked_pairs=blocked)

if __name__ == "__main__":
    unittest.main()
