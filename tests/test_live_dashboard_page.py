from __future__ import annotations

import unittest
from importlib.resources import files

from app.dashboard.server import _dashboard_html


class LiveDashboardPageTests(unittest.TestCase):
    def test_dashboard_serves_current_live_monitor_page(self) -> None:
        html = _dashboard_html()

        self.assertIn('id="tabLive"', html)
        self.assertIn("Live Monitor", html)
        self.assertIn('id="livePortfolioEq"', html)
        self.assertIn('id="liveAllocatedCapital"', html)

    def test_dashboard_normalizes_pair_api_before_array_operations(self) -> None:
        javascript = (
            files("app.dashboard.static")
            .joinpath("dashboard.js")
            .read_text(encoding="utf-8")
        )

        self.assertIn("function normalizePairRecords(payload)", javascript)
        self.assertIn("allPairs = normalizePairRecords(data);", javascript)


if __name__ == "__main__":
    unittest.main()
