from __future__ import annotations

import unittest
import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from app.config import Settings, RiskSettings
from app.persistence.paper_state import PaperSessionStore
from app.live.paper_loop import PaperTradingLoop


class PaperSessionPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.file_path = Path("data/test_paper_state.json")
        if self.file_path.exists():
            self.file_path.unlink()
        
        self.store = PaperSessionStore(file_path=str(self.file_path))
        
        self.settings = Settings(
            paper_starting_equity=Decimal("10000"),
        )
        self.loop = PaperTradingLoop(
            self.settings,
            state_store=MagicMock(), # don't need real DB for session tests
            session_store=self.store
        )

    def tearDown(self) -> None:
        if self.file_path.exists():
            self.file_path.unlink()
        tmp_path = self.file_path.with_name(f"{self.file_path.name}.tmp")
        if tmp_path.exists():
            tmp_path.unlink()
        recovery_path = self.file_path.with_name(f"{self.file_path.name}.recovery")
        if recovery_path.exists():
            recovery_path.unlink()
        for path in self.file_path.parent.glob(f".{self.file_path.name}.*.tmp"):
            path.unlink()

    def test_save_and_load_session(self) -> None:
        state = {
            "candle_count": 42,
            "equity_history": [{"t": 1000, "equity": "10050"}],
            "position_entry_candle": {"B-BTC_USDT": 10},
            "realized_pnl": Decimal("50.5"),
        }
        self.store.save_session(state)
        
        loaded = self.store.load_session()
        self.assertEqual(loaded["candle_count"], 42)
        self.assertEqual(loaded["realized_pnl"], Decimal("50.5"))
        self.assertEqual(loaded["position_entry_candle"]["B-BTC_USDT"], 10)

    def test_save_session_is_atomic_when_serialization_fails(self) -> None:
        self.store.save_session({"candle_count": 7})
        before = self.file_path.read_text(encoding="utf-8")

        with self.assertRaises(TypeError):
            self.store.save_session({"bad": object()})

        self.assertEqual(self.file_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.store.load_session()["candle_count"], 7)

    def test_save_session_retries_when_windows_replace_is_temporarily_locked(self) -> None:
        real_replace = os.replace
        attempts = {"main": 0}

        def flaky_replace(source, target):
            if Path(target) == self.file_path and attempts["main"] < 2:
                attempts["main"] += 1
                raise PermissionError("temporarily locked")
            return real_replace(source, target)

        with (
            patch("app.persistence.paper_state.os.replace", side_effect=flaky_replace),
            patch("app.persistence.paper_state.time.sleep"),
        ):
            self.store.save_session({"candle_count": 88})

        self.assertEqual(attempts["main"], 2)
        self.assertEqual(self.store.load_session()["candle_count"], 88)

    def test_save_session_uses_recovery_when_main_file_stays_locked(self) -> None:
        real_replace = os.replace

        def locked_main_replace(source, target):
            if Path(target) == self.file_path:
                raise PermissionError("main file locked")
            return real_replace(source, target)

        with (
            patch("app.persistence.paper_state.os.replace", side_effect=locked_main_replace),
            patch("app.persistence.paper_state.time.sleep"),
        ):
            self.store.save_session({"candle_count": 99})

        self.assertEqual(self.store.load_session()["candle_count"], 99)
        self.assertTrue(self.file_path.with_name(f"{self.file_path.name}.recovery").exists())

    def test_loop_restores_from_store(self) -> None:
        state = {
            "candle_count": 100,
            "equity_history": [{"t": 5000, "equity": "11000"}],
            "position_entry_candle": {"B-ETH_USDT": 50},
            "entries_this_parent_candle": {"B-ETH_USDT": 1},
            "current_parent_open_ms": {"B-ETH_USDT": 900000}
        }
        self.store.save_session(state)

        # New loop should restore
        new_loop = PaperTradingLoop(self.settings, session_store=self.store)

        self.assertEqual(new_loop.candle_count, 100)
        self.assertEqual(len(new_loop.equity_history), 1)
        self.assertEqual(new_loop._position_entry_candle["B-ETH_USDT"], 50)
        self.assertEqual(new_loop._entries_this_parent_candle["B-ETH_USDT"], 1)

        self.assertEqual(new_loop._current_parent_open_ms["B-ETH_USDT"], 900000)

    def test_loop_restores_paper_safety_cooldowns(self) -> None:
        state_store = MagicMock()
        state_store.load.return_value = None
        self.loop._pair_cooldown_until_ms = {"B-BTC_USDT": 123000}
        self.loop._same_direction_cooldown_until_ms = {("B-BTC_USDT", "long"): 456000}
        self.loop._same_direction_reversal_cooldown_until_ms = {("B-ETH_USDT", "short"): 654000}
        self.loop._pair_recent_net_pnls = {"B-ALICE_USDT": [Decimal("-10"), Decimal("5")]}
        self.loop._global_loss_cooldown_until_ms = 789000
        self.loop._consecutive_losing_trades = 2

        self.loop._save_session()

        restored = PaperTradingLoop(
            self.settings,
            state_store=state_store,
            session_store=self.store,
        )

        self.assertEqual(restored._pair_cooldown_until_ms["B-BTC_USDT"], 123000)
        self.assertEqual(
            restored._same_direction_cooldown_until_ms[("B-BTC_USDT", "long")],
            456000,
        )
        self.assertEqual(
            restored._same_direction_reversal_cooldown_until_ms[("B-ETH_USDT", "short")],
            654000,
        )
        self.assertEqual(
            restored._pair_recent_net_pnls["B-ALICE_USDT"],
            [Decimal("-10"), Decimal("5")],
        )
        self.assertEqual(restored._global_loss_cooldown_until_ms, 789000)
        self.assertEqual(restored._consecutive_losing_trades, 2)

    def test_loop_passes_trailing_stop_settings_to_broker(self) -> None:
        state_store = MagicMock()
        state_store.load.return_value = None
        settings = Settings(
            paper_starting_equity=Decimal("10000"),
            risk=RiskSettings(
                trailing_stop_enabled=True,
                trailing_stop_activation_pct=Decimal("1.25"),
                trailing_stop_distance_pct=Decimal("2.5"),
            ),
        )

        loop = PaperTradingLoop(
            settings,
            state_store=state_store,
            session_store=self.store,
        )

        self.assertTrue(loop.broker.trailing_stop_enabled)
        self.assertEqual(loop.broker.trailing_stop_activation_pct, Decimal("1.25"))
        self.assertEqual(loop.broker.trailing_stop_distance_pct, Decimal("2.5"))

    def test_missing_file_initializes_cleanly(self) -> None:
        if self.file_path.exists(): self.file_path.unlink()
        
        new_loop = PaperTradingLoop(self.settings, session_store=self.store)
        
        self.assertEqual(new_loop.candle_count, 0)
        self.assertEqual(len(new_loop.equity_history), 0)

    def test_reset_clears_files(self) -> None:
        self.store.save_session({"data": 1})
        self.assertTrue(self.file_path.exists())
        
        self.store.clear()
        self.assertFalse(self.file_path.exists())


if __name__ == "__main__":
    unittest.main()
