from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from app.config import Settings
from app.main import build_parser, live_run_command


class LiveCliDefaultsTests(unittest.TestCase):
    def test_live_run_defaults_to_one_hour_with_five_minute_execution(self) -> None:
        args = build_parser().parse_args(
            [
                "live-run",
                "--pair",
                "B-STO_USDT",
                "--allocated-capital",
                "1000",
            ]
        )

        self.assertEqual(args.interval, "1h")
        self.assertEqual(args.execution_interval, "5m")
        self.assertEqual(args.equity, Decimal("1000"))
        self.assertFalse(args.dry_run)

    def test_live_run_accepts_fib_ma_for_explicit_dry_run(self) -> None:
        args = build_parser().parse_args(
            [
                "live-run",
                "--pair",
                "B-STO_USDT",
                "--strategy",
                "fib_ma_pullback",
                "--allocated-capital",
                "1000",
                "--dry-run",
            ]
        )

        self.assertEqual(args.strategy, "fib_ma_pullback")
        self.assertTrue(args.dry_run)

    def test_live_run_accepts_capital_per_pair(self) -> None:
        args = build_parser().parse_args(
            [
                "live-run",
                "--pairs",
                "B-BTC_USDT,B-SOL_USDT,B-ETH_USDT",
                "--capital-per-pair",
                "1000",
            ]
        )

        self.assertIsNone(args.equity)
        self.assertEqual(args.capital_per_pair, Decimal("1000"))

    def test_live_run_accepts_pair_specific_leverage(self) -> None:
        args = build_parser().parse_args(
            [
                "live-run",
                "--pairs",
                "B-BTC_USDT,B-SOL_USDT,B-ETH_USDT",
                "--capital-per-pair",
                "1000",
                "--leverage",
                "4",
                "--leverage-by-pair",
                "B-BTC_USDT=2,B-SOL_USDT=6",
            ]
        )

        self.assertEqual(
            args.leverage_by_pair,
            {"B-BTC_USDT": Decimal("2"), "B-SOL_USDT": Decimal("6")},
        )

    def test_capital_per_pair_sets_total_allocation_and_pair_cap(self) -> None:
        with patch("app.main.load_settings", return_value=Settings()):
            with patch("app.main.configure_logging"):
                with patch("app.live.live_loop.LiveTradingLoop") as loop_class:
                    loop_instance = loop_class.return_value

                    live_run_command(
                        pair=None,
                        pairs_csv="B-BTC_USDT,B-SOL_USDT,B-ETH_USDT",
                        interval="1h",
                        execution_interval="5m",
                        strategy_name="hybrid_meta_v2",
                        equity=None,
                        leverage=Decimal("5"),
                        dry_run=True,
                        capital_per_pair=Decimal("1000"),
                    )

        loop_class.assert_called_once()
        kwargs = loop_class.call_args.kwargs
        self.assertEqual(kwargs["starting_equity"], Decimal("3000"))
        self.assertEqual(kwargs["settings"].risk.max_margin_per_pair, Decimal("1000"))
        loop_instance.run.assert_called_once()
        loop_instance.stop.assert_called_once()

    def test_pair_specific_leverage_is_passed_to_live_loop(self) -> None:
        with patch("app.main.load_settings", return_value=Settings()):
            with patch("app.main.configure_logging"):
                with patch("app.live.live_loop.LiveTradingLoop") as loop_class:
                    live_run_command(
                        pair=None,
                        pairs_csv="B-BTC_USDT,B-SOL_USDT,B-ETH_USDT",
                        interval="1h",
                        execution_interval="5m",
                        strategy_name="hybrid_meta_v2",
                        equity=None,
                        leverage=Decimal("4"),
                        dry_run=True,
                        capital_per_pair=Decimal("1000"),
                        leverage_by_pair={
                            "B-BTC_USDT": Decimal("2"),
                            "B-SOL_USDT": Decimal("6"),
                        },
                    )

        self.assertEqual(
            loop_class.call_args.kwargs["leverage"],
            {
                "B-BTC_USDT": Decimal("2"),
                "B-SOL_USDT": Decimal("6"),
                "B-ETH_USDT": Decimal("4"),
            },
        )

    def test_pair_specific_leverage_rejects_unknown_pair(self) -> None:
        with patch("app.main.load_settings", return_value=Settings()):
            with patch("app.main.configure_logging"):
                with self.assertRaises(SystemExit) as ctx:
                    live_run_command(
                        pair=None,
                        pairs_csv="B-BTC_USDT,B-SOL_USDT",
                        interval="1h",
                        execution_interval="5m",
                        strategy_name="hybrid_meta_v2",
                        equity=None,
                        leverage=Decimal("4"),
                        dry_run=True,
                        capital_per_pair=Decimal("1000"),
                        leverage_by_pair={"B-ETH_USDT": Decimal("6")},
                    )

        self.assertIn("not in this run", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
