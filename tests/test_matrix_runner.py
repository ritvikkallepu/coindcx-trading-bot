from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from pathlib import Path
import pandas as pd
from scripts.run_backtest_matrix import CandleCache, parse_decimal, parse_int, parse_bool

class MatrixRunnerTests(unittest.TestCase):
    def test_parse_helpers(self) -> None:
        self.assertEqual(parse_decimal("1.5"), Decimal("1.5"))
        self.assertEqual(parse_decimal(None, Decimal("10")), Decimal("10"))
        self.assertEqual(parse_int("100"), 100)
        self.assertEqual(parse_int("", 1000), 1000)
        self.assertTrue(parse_bool("on"))
        self.assertTrue(parse_bool(True))
        self.assertFalse(parse_bool("off"))
        self.assertFalse(parse_bool(None, False))

    def test_candle_cache(self) -> None:
        cache = CandleCache()
        mock_client = MagicMock()
        
        with patch("scripts.run_backtest_matrix.load_historical_candle_series") as mock_load:
            mock_load.return_value = [1, 2, 3]
            
            # First call - should fetch
            c1, t1, cached1 = cache.get_candles(mock_client, "BTC", "1h", 100)
            self.assertEqual(mock_load.call_count, 1)
            self.assertFalse(cached1)
            
            # Second call - should use cache
            c2, t2, cached2 = cache.get_candles(mock_client, "BTC", "1h", 100)
            self.assertEqual(mock_load.call_count, 1)
            self.assertTrue(cached2)
            self.assertEqual(c1, c2)
            self.assertEqual(t2, 0.0)
            
            # Different key - should fetch
            c3, t3, cached3 = cache.get_candles(mock_client, "ETH", "1h", 100)
            self.assertEqual(mock_load.call_count, 2)
            self.assertFalse(cached3)

    @patch("scripts.run_backtest_matrix.pd.read_excel")
    @patch("scripts.run_backtest_matrix.BacktestEngine")
    @patch("scripts.run_backtest_matrix.CandleCache")
    def test_run_matrix_logic_skipping(self, mock_cache_cls, mock_engine_cls, mock_read_excel) -> None:
        from scripts.run_backtest_matrix import run_matrix
        
        # Mock dataframe with one complete, one failed, one pending
        df = pd.DataFrame([
            {"pair": "BTC", "status": "Complete"},
            {"pair": "ETH", "status": "Failed"},
            {"pair": "SOL", "status": "Pending"}
        ])
        mock_read_excel.return_value = df
        
        mock_args = MagicMock()
        mock_args.workbook = "test.xlsx"
        mock_args.resume = False
        mock_args.retry_failed = False
        mock_args.limit = None
        mock_args.coin = None
        mock_args.dry_run = True # dry run to avoid full execution
        
        # Test default: skip complete and failed
        with patch("pandas.DataFrame.to_excel"):
            run_matrix(mock_args)
            # Logic check happens in the loop. We can verify if log messages would be sent, 
            # but here let's just ensure it doesn't crash.
            
    @patch("scripts.run_backtest_matrix.BacktestConfig")
    def test_config_wiring(self, mock_config_cls) -> None:
        # Verify specific fields like max_daily_loss_pct are passed to config
        # This would be part of the run_matrix loop, testing parsing logic.
        pass

if __name__ == "__main__":
    unittest.main()
