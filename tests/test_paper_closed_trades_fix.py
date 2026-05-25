from __future__ import annotations

import unittest
import json
import csv
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock
from dataclasses import replace

from app.config import Settings, RiskSettings
from app.data.candle_builder import OHLCVCandle, CandleSeries
from app.live.paper_loop import PaperTradingLoop, get_live_state
from app.strategies.base import SignalAction, SignalDirection, StrategySignal
from app.broker.models import PaperFill, PaperOrderSide, PaperPosition
from app.broker.paper import PaperBroker
from app.live.summary_logger import PaperTradingSummaryLogger


def _candle(interval: str, open_time_ms: int, close: Decimal = Decimal("100"), low: Decimal | None = None, pair: str = "B-BTC_USDT") -> OHLCVCandle:
    from app.data.candle_builder import interval_to_ms
    interval_ms = interval_to_ms(interval)
    return OHLCVCandle(
        pair=pair,
        interval=interval,
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + interval_ms - 1,
        open=close,
        high=close + Decimal("1"),
        low=low if low is not None else close - Decimal("1"),
        close=close,
        volume=Decimal("100"),
    )


class PaperClosedTradesFixTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.persistence.paper_state import PaperStateStore, PaperSessionStore
        self.db_path = Path("test_paper_state_fix.db")
        self.session_path = Path("data/test_paper_session_fix.json")
        self.csv_path = Path("test_paper_trades_fix.csv")

        if self.db_path.exists(): self.db_path.unlink()
        if self.session_path.exists(): self.session_path.unlink()
        if self.csv_path.exists(): self.csv_path.unlink()

        self.state_store = PaperStateStore(str(self.db_path))
        self.session_store = PaperSessionStore(str(self.session_path))

        self.settings = Settings(
            paper_starting_equity=Decimal("10000"),
            risk=RiskSettings(
                max_risk_per_trade_pct=Decimal("1"),
                slippage_pct=Decimal("0"),
                stop_slippage_pct=Decimal("0"), # Task: 0 stop slippage
                fee_gst_rate=Decimal("0")
            )
        )
        # Use test paths
        self.loop = PaperTradingLoop(
            self.settings, 
            strategy_name="adaptive_hybrid",
            state_store=self.state_store,
            session_store=self.session_store
        )
        self.loop.summary_logger.csv_path = self.csv_path

        self.loop.client.get_candles = MagicMock(return_value={"data": []})
        self.loop.gap_guard = MagicMock()
        self.loop.gap_guard.check.return_value.has_gap = False
        self.loop._current_interval = "15m"

    def tearDown(self) -> None:
        self.loop.state_store.close()
        if self.db_path.exists(): self.db_path.unlink()
        if self.session_path.exists(): self.session_path.unlink()
        if self.csv_path.exists(): self.csv_path.unlink()


    def test_trade_closure_updates_dashboard_and_csv(self) -> None:
        # 1. Warm up with one candle
        pair = "B-BTC_USDT"
        self.loop._watchlist = [pair]
        self.loop.series = {pair: CandleSeries()}
        from app.data.gap_guard import CandleGapGuard
        self.loop.gap_guards = {pair: CandleGapGuard("15m")}
        self.loop.series[pair].add(_candle("15m", 0))

        # 2. Mock strategy to enter long
        signal = StrategySignal(
            strategy_name="S1", pair=pair, interval="15m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="entry", timestamp_ms=900000,
            entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit=Decimal("110")
        )
        self.loop.strategy_engine.evaluate = MagicMock(return_value=[signal])

        # 3. Process entry candle
        self.loop._on_candle(_candle("15m", 900000, Decimal("100"), pair=pair))

        self.assertEqual(len(self.loop.broker.positions), 1)

        self.assertEqual(len(self.loop.broker.fills), 1)
        state = get_live_state()
        self.assertEqual(state["total_fills"], 1)

        # 4. Mock strategy to hold (don't exit via signal)
        self.loop.strategy_engine.evaluate = MagicMock(return_value=[])

        # 5. Process exit candle (hits stop loss at 95)
        # Low is 94, so SL at 95 is hit
        self.loop._on_candle(_candle("15m", 1800000, Decimal("94"), low=Decimal("90")))

        # 6. Verify Broker State
        self.assertEqual(len(self.loop.broker.positions), 0)
        self.assertEqual(len(self.loop.broker.fills), 2)
        self.assertLess(self.loop.broker.realized_pnl, 0)

        # 7. Verify Dashboard State
        state = get_live_state()
        self.assertEqual(state["total_fills"], 2)
        self.assertEqual(state["open_positions"], 0)
        self.assertNotEqual(state["realized_pnl"], "0")

        # 8. Verify CSV Content
        self.assertTrue(self.csv_path.exists())
        with open(self.csv_path, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.assertEqual(len(rows), 1)
            trade = rows[0]
            self.assertEqual(trade["pair"], "B-BTC_USDT")
            self.assertEqual(trade["direction"], "long")
            self.assertEqual(trade["entry_price"], "100")
            # It gapped to 94 (candle open), so exit price should be 94
            self.assertEqual(trade["exit_price"], "94")
            self.assertEqual(trade["stop_loss"], "95")
            self.assertEqual(trade["take_profit"], "110")
            self.assertEqual(trade["exit_reason"], "stop_loss")
            self.assertNotEqual(trade["net_pnl"], "0")
            self.assertIn("position_notional", trade)
            self.assertGreater(Decimal(trade["position_notional"]), Decimal("0"))
            self.assertEqual(trade["notional_currency"], "INR")
            self.assertEqual(trade["price_quote_currency"], "USDT")
            self.assertEqual(trade["quote_to_margin_rate"], "98")
            self.assertNotEqual(trade["risk_percent_used"], "")

    def test_breakeven_stop_close_is_logged_even_with_zero_realized_pnl(self) -> None:
        pair = "B-BTC_USDT"
        self.loop._watchlist = [pair]
        self.loop.series = {pair: CandleSeries()}
        from app.data.gap_guard import CandleGapGuard
        self.loop.gap_guards = {pair: CandleGapGuard("15m")}
        self.loop.series[pair].add(_candle("15m", 0, Decimal("100"), pair=pair))

        signal = StrategySignal(
            strategy_name="S1", pair=pair, interval="15m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="entry", timestamp_ms=900000,
            entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit=Decimal("110")
        )
        self.loop.strategy_engine.evaluate = MagicMock(return_value=[signal])
        self.loop._on_candle(_candle("15m", 900000, Decimal("100"), pair=pair))
        position = self.loop.broker.positions[pair]
        self.loop.broker.positions[pair] = replace(
            position,
            stop_loss=Decimal("100"),
            metadata={**position.metadata, "stop_type": "breakeven"},
        )

        self.loop.strategy_engine.evaluate = MagicMock(return_value=[])
        self.loop._on_candle(_candle("15m", 1800000, Decimal("101"), low=Decimal("99"), pair=pair))

        self.assertEqual(len(self.loop.broker.positions), 0)
        self.assertEqual(self.loop.broker.fills[-1].realized_pnl, Decimal("0"))
        self.assertTrue(self.csv_path.exists())
        with open(self.csv_path, mode="r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_reason"], "breakeven_stop")

    def test_trade_mapping_uses_position_leverage_for_margin_and_roe(self) -> None:
        pair = "B-BTC_USDT"
        position = PaperPosition(
            pair=pair,
            direction=SignalDirection.LONG,
            quantity=Decimal("10"),
            entry_price=Decimal("100"),
            leverage=Decimal("5"),
            opened_at_ms=0,
            updated_at_ms=0,
            strategy_name="S1",
            stop_loss=Decimal("95"),
            take_profit=Decimal("120"),
            metadata={
                "account_equity_at_entry": Decimal("100000"),
                "entry_fee": Decimal("0"),
            },
        )
        fill = PaperFill(
            fill_id="f1",
            order_id="o1",
            pair=pair,
            side=PaperOrderSide.SELL,
            quantity=Decimal("10"),
            price=Decimal("110"),
            fee=Decimal("0"),
            timestamp_ms=900000,
            realized_pnl=Decimal("9800"),
            metadata={
                "exit_trigger_type": "stop_loss",
                "stop_type": "atr",
                "atr_stop_enabled": True,
            },
        )

        trade = self.loop._map_fill_to_trade_dict(
            fill,
            _candle("15m", 900000, Decimal("110"), pair=pair),
            position=position,
        )

        self.assertEqual(trade["leverage"], "5")
        self.assertEqual(Decimal(trade["position_notional"]), Decimal("98000"))
        self.assertEqual(Decimal(trade["margin_used"]), Decimal("19600"))
        self.assertEqual(Decimal(trade["required_margin"]), Decimal("19600"))
        self.assertEqual(Decimal(trade["net_roe_pct"]), Decimal("50.0"))
        self.assertEqual(trade["exit_reason"], "dynamic_atr_stop")

    def test_paper_state_serialization_handles_enums(self) -> None:
        # Manually create a position and save it
        pos = self.loop.state_store._restore_position({
            "pair": "B-ETH_USDT",
            "direction": "long",
            "quantity": "1.0",
            "entry_price": "2000",
            "leverage": "5",
            "opened_at_ms": 1000,
            "updated_at_ms": 1000,
            "strategy_name": "S",
            "stop_loss": "1900",
            "take_profit": "2200",
            "metadata": {}
        })
        self.loop.broker.positions["B-ETH_USDT"] = pos
        
        # This should NOT crash with Enum serialization error
        try:
            self.loop.broker._save_state()
        except Exception as exc:
            self.fail(f"_save_state raised {type(exc).__name__}: {exc}")

        # Reload and verify
        new_broker = PaperBroker(starting_equity=Decimal("10000"), state_store=self.loop.state_store)
        self.assertEqual(len(new_broker.positions), 1)
        reloaded = new_broker.positions["B-ETH_USDT"]
        self.assertEqual(reloaded.direction, SignalDirection.LONG)
        self.assertEqual(reloaded.entry_price, Decimal("2000"))

    def test_summary_logger_migrates_old_header_before_writing_new_inr_fields(self) -> None:
        self.csv_path.write_text(
            "timestamp,pair,interval,strategy,direction,entry_price,exit_price,"
            "stop_loss,take_profit,position_size,gross_pnl,fees,net_pnl,"
            "net_pnl_pct,equity_after,exit_reason,hold_duration_candles\n"
            "old,B-BSB_USDT,1m,S,long,1,2,,,10,1,0.1,0.9,1,1000,signal,1\n",
            encoding="utf-8",
        )
        logger = PaperTradingSummaryLogger(csv_path=str(self.csv_path))
        logger.on_trade_closed(
            {
                "timestamp": "new",
                "pair": "B-BSB_USDT",
                "interval": "1m",
                "strategy": "S",
                "direction": "long",
                "entry_price": "1",
                "exit_price": "2",
                "position_size": "10",
                "quantity_unit": "BSB",
                "position_notional": "980",
                "notional_currency": "INR",
                "price_quote_currency": "USDT",
                "quote_to_margin_rate": "98",
                "risk_percent_used": "3",
                "gross_pnl": "980",
                "fees": "0.5",
                "net_pnl": "979.5",
                "net_pnl_pct": "100",
                "equity_after": "1979.5",
                "exit_reason": "take_profit",
                "hold_duration_candles": "1",
            }
        )

        with self.csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        self.assertIn("position_notional", rows[0])
        self.assertEqual(rows[0]["legacy_currency_math"], "true")
        self.assertEqual(rows[1]["position_notional"], "980")
        self.assertEqual(rows[1]["quote_to_margin_rate"], "98")

    def test_summary_logger_skips_duplicate_closed_trade_rows(self) -> None:
        logger = PaperTradingSummaryLogger(csv_path=str(self.csv_path))
        trade = {
            "fill_id": "fill-1",
            "order_id": "order-1",
            "timestamp": "2026-05-22T21:32:59Z",
            "pair": "B-EDEN_USDT",
            "interval": "1m",
            "strategy": "S",
            "direction": "short",
            "entry_price": "0.11648173",
            "exit_price": "0.12292871",
            "position_size": "14326.211685",
            "quantity_unit": "EDEN",
            "position_notional": "163536.70",
            "notional_currency": "INR",
            "price_quote_currency": "USDT",
            "quote_to_margin_rate": "98",
            "risk_percent_used": "8",
            "gross_pnl": "-9051.36",
            "fees": "101.82",
            "net_pnl": "-9153.19",
            "net_pnl_pct": "-5.59",
            "equity_after": "102901",
            "exit_reason": "stop_loss",
            "hold_duration_candles": "317",
        }

        logger.on_trade_closed(trade)
        logger.on_trade_closed({**trade, "equity_after": "102762", "hold_duration_candles": "0"})

        with self.csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["hold_duration_candles"], "317")


if __name__ == "__main__":
    unittest.main()
