from __future__ import annotations

import unittest
import csv
from pathlib import Path
from decimal import Decimal
import time
from app.config import Settings
from app.live.paper_loop import PaperTradingLoop
from app.data.candle_builder import OHLCVCandle

class TestPaperAudit(unittest.TestCase):
    def setUp(self):
        self.audit_path = Path("data/paper_intrabar_audit.csv")
        if self.audit_path.exists():
            # Backup current audit file just in case
            self.backup_path = Path("data/paper_intrabar_audit.csv.bak")
            self.audit_path.rename(self.backup_path)

    def tearDown(self):
        if self.audit_path.exists():
            self.audit_path.unlink()
        if hasattr(self, "backup_path") and self.backup_path.exists():
            self.backup_path.rename(self.audit_path)

    def test_audit_timestamps_are_nonzero(self):
        settings = Settings(
            trading_mode="paper",
            strategy_interval="5m",
            execution_interval="1m",
            paper_intrabar_enabled=True,
            audit_max_file_mb=100
        )
        
        loop = PaperTradingLoop(settings, strategy_name="hybrid_meta_v2", execution_mode="paper")
        
        # We need to simulate a candle that produces an audit row.
        # _handle_execution_candle writes the row if audit_signal is produced.
        # But wait, we can just call _write_audit_row directly.
        from app.strategies.base import StrategySignal, SignalAction, SignalDirection
        
        now_ms = int(time.time() * 1000)
        
        candle = OHLCVCandle(
            pair="B-BTC_USDT",
            interval="1m",
            open_time_ms=now_ms - 60000,
            close_time_ms=now_ms,
            open=Decimal("50000"),
            high=Decimal("50100"),
            low=Decimal("49900"),
            close=Decimal("50050"),
            volume=Decimal("10"),
            is_closed=True
        )
        
        signal = StrategySignal(
            strategy_name="hybrid_meta_v2",
            pair="B-BTC_USDT",
            interval="1m",
            action=SignalAction.ENTER_LONG,
            direction=SignalDirection.LONG,
            confidence=Decimal("0.8"),
            reason="test",
            timestamp_ms=candle.close_time_ms,
            metadata={"entry_type": "test_entry"}
        )
        
        loop._write_audit_row(candle, signal)
        
        self.assertTrue(self.audit_path.exists())
        
        with open(self.audit_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            row = next(reader)
            
            timestamp_str = row[0]
            # Should not be epoch zero (1970-01-01)
            self.assertNotIn("1970-01-01", timestamp_str)
            
            # Should match the candle close_time_ms converted to isoformat
            from datetime import datetime, timezone
            expected_iso = datetime.fromtimestamp(candle.close_time_ms / 1000, tz=timezone.utc).isoformat()
            self.assertEqual(timestamp_str, expected_iso)

if __name__ == "__main__":
    unittest.main()
