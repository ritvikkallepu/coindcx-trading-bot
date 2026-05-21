from __future__ import annotations

import csv
import logging
from decimal import Decimal
from pathlib import Path
from datetime import datetime, timezone


class PaperTradingSummaryLogger:
    def __init__(self, csv_path: str = "paper_trades.csv", summary_every_n_candles: int = 10) -> None:
        self.csv_path = Path(csv_path)
        self.summary_every_n_candles = summary_every_n_candles
        self.logger = logging.getLogger(__name__)
        
        self.closed_trades_pnl: list[Decimal] = []
        self.peak_equity = Decimal("0")
        self.warning_fired = False
        
        self._header = [
            "timestamp", "pair", "interval", "strategy", "direction", 
            "entry_price", "exit_price", "position_size", "gross_pnl", 
            "fees", "net_pnl", "net_pnl_pct", "equity_after", 
            "exit_reason", "hold_duration_candles"
        ]

    def on_trade_closed(self, trade: dict) -> None:
        file_exists = self.csv_path.exists()
        
        with open(self.csv_path, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self._header)
            if not file_exists:
                writer.writeheader()
            
            # Format row data
            row = {k: str(trade.get(k, "")) for k in self._header}
            writer.writerow(row)
        
        # Update rolling win rate tracking
        net_pnl = Decimal(str(trade.get("net_pnl", "0")))
        self.closed_trades_pnl.append(net_pnl)
        if len(self.closed_trades_pnl) > 100: # Keep a buffer but on_candle only uses last 20
            self.closed_trades_pnl.pop(0)

    def on_candle(self, candle_number: int, equity: Decimal, open_positions: int, 
                  daily_pnl: Decimal) -> None:
        if candle_number % self.summary_every_n_candles == 0:
            win_rate = Decimal("0")
            recent = self.closed_trades_pnl[-20:]
            if recent:
                wins = sum(1 for pnl in recent if pnl > 0)
                win_rate = (Decimal(wins) / Decimal(len(recent))) * Decimal("100")
                
            self.logger.info(
                f"[PAPER] candle={candle_number} | equity={equity:.2f} | "
                f"open={open_positions} | today_pnl={daily_pnl:.2f} | "
                f"win_rate={win_rate:.1f}% (last 20 trades)"
            )

    def check_drawdown(self, current_equity: Decimal) -> None:
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity
            # Reset warning if we've recovered above 95% of the new peak
            # Actually instructions say "resets when equity recovers above 95% of peak"
            # But if we just hit a new peak, we are at 100% of peak, so we definitely reset.
            self.warning_fired = False
            return

        if self.peak_equity <= 0:
            return

        drawdown_pct = ((self.peak_equity - current_equity) / self.peak_equity) * Decimal("100")
        
        if drawdown_pct > Decimal("5"):
            if not self.warning_fired:
                self.logger.warning(
                    f"DRAWDOWN ALERT: peaked at {self.peak_equity:.2f}, "
                    f"now {current_equity:.2f} ({drawdown_pct:.1f}% drawdown)"
                )
                self.warning_fired = True
        else:
            recovery_threshold = self.peak_equity * Decimal("0.95")
            if current_equity >= recovery_threshold:
                self.warning_fired = False
