from __future__ import annotations

import csv
import logging
from decimal import Decimal
from pathlib import Path
from datetime import datetime, timezone
from tempfile import NamedTemporaryFile


class PaperTradingSummaryLogger:
    def __init__(self, csv_path: str = "paper_trades.csv", summary_every_n_candles: int = 10) -> None:
        self.csv_path = Path(csv_path)
        self.summary_every_n_candles = summary_every_n_candles
        self.logger = logging.getLogger(__name__)
        
        self.closed_trades_pnl: list[Decimal] = []
        self.peak_equity = Decimal("0")
        self.warning_fired = False
        
        self._header = [
            "fill_id", "order_id",
            "timestamp", "pair", "interval", "strategy", "direction", 
            "entry_price", "exit_price", "stop_loss", "take_profit",
            "position_size", "quantity_unit",
            "position_notional", "entry_notional", "exit_notional", 
            "leverage", "margin_used",
            "notional_currency", "price_quote_currency",
            "quote_to_margin_rate", "unit_contract_value",
            "risk_percent_used", "risk_multiplier", "risk_base_mode",
            "risk_base_amount", "planned_risk_amount", "required_margin",
            "available_equity", "margin_ok", "account_blown",
            "fee_type", "fee_rate", "fee_gst_rate", "effective_fee_rate",
            "entry_fee", "exit_fee", "total_fees",
            "gross_pnl", "fees", "net_pnl", "net_pnl_pct", 
            "gross_roe_pct", "net_roe_pct", "account_equity_at_entry", "account_impact_pct",
            "equity_after",
            "amount_locked_from_trade", "tradable_equity_after_trade",
            "locked_profit_after_trade", "daily_loss_after_trade",
            "exit_reason", "hold_duration_candles", "legacy_currency_math",
        ]

    def on_trade_closed(self, trade: dict) -> None:
        self._ensure_header()
        trade_key = _trade_key(trade)
        if trade_key in self._existing_trade_keys():
            self.logger.warning(
                "Skipping duplicate paper closed-trade row for %s.",
                "|".join(trade_key),
            )
            return
        
        with open(self.csv_path, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self._header)
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

    def _ensure_header(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.csv_path.exists() or self.csv_path.stat().st_size == 0:
            with open(self.csv_path, mode="w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self._header).writeheader()
            return

        with open(self.csv_path, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing_header = list(reader.fieldnames or [])
            if existing_header == self._header:
                return
            rows = list(reader)

        # Migrate old paper-trade CSVs in place so newly added INR/risk fields
        # are not silently dropped when the file already exists.
        with NamedTemporaryFile(
            mode="w",
            newline="",
            encoding="utf-8",
            delete=False,
            dir=str(self.csv_path.parent),
        ) as tmp:
            writer = csv.DictWriter(tmp, fieldnames=self._header)
            writer.writeheader()
            for row in rows:
                migrated = {key: str(row.get(key, "") or "") for key in self._header}
                if "quote_to_margin_rate" not in existing_header:
                    migrated["legacy_currency_math"] = "true"
                writer.writerow(migrated)
            tmp_path = Path(tmp.name)

        tmp_path.replace(self.csv_path)

    def _existing_trade_keys(self) -> set[tuple[str, ...]]:
        if not self.csv_path.exists() or self.csv_path.stat().st_size == 0:
            return set()
        with open(self.csv_path, mode="r", newline="", encoding="utf-8") as f:
            return {_trade_key(row) for row in csv.DictReader(f)}


def _trade_key(row: dict) -> tuple[str, ...]:
    fill_id = str(row.get("fill_id") or "").strip()
    if fill_id:
        return (
            "fill_id",
            fill_id,
            str(row.get("order_id") or "").strip(),
            str(row.get("timestamp") or "").strip(),
            str(row.get("pair") or "").strip(),
        )
    return (
        "composite",
        str(row.get("timestamp") or "").strip(),
        str(row.get("pair") or "").strip(),
        str(row.get("direction") or "").strip(),
        str(row.get("entry_price") or "").strip(),
        str(row.get("exit_price") or "").strip(),
        str(row.get("position_size") or row.get("quantity") or "").strip(),
        str(row.get("net_pnl") or "").strip(),
        str(row.get("exit_reason") or row.get("reason") or "").strip(),
    )
