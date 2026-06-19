"""Tests for over-deployment trim planner."""
from __future__ import annotations

import unittest

from overdeployment_trim import plan_overdeployment_trims


class OverdeploymentTrimTests(unittest.TestCase):
    def test_plans_trim_from_largest_holdings(self) -> None:
        states = {
            "TSM": {"held_quantity": 73},
            "TSLA": {"held_quantity": 69},
            "AMD": {"held_quantity": 49},
            "CRWD": {"held_quantity": 38},
            "AAPL": {"held_quantity": 1},
        }
        prices = {
            "TSM": 460.0,
            "TSLA": 400.0,
            "AMD": 535.0,
            "CRWD": 685.0,
            "AAPL": 298.0,
        }
        trims, marked, target = plan_overdeployment_trims(
            states,
            list(prices.keys()),
            prices,
            capital_at_risk=100_000.0,
            target_pct=0.98,
        )
        self.assertGreater(marked, target)
        self.assertGreater(len(trims), 0)
        tickers = [t.ticker for t in trims]
        self.assertIn("TSM", tickers)

    def test_no_trim_when_within_cap(self) -> None:
        states = {"AAPL": {"held_quantity": 10}}
        prices = {"AAPL": 200.0}
        trims, _, _ = plan_overdeployment_trims(
            states,
            ["AAPL"],
            prices,
            capital_at_risk=100_000.0,
        )
        self.assertEqual(trims, [])


if __name__ == "__main__":
    unittest.main()
