"""Tests for deployable cash alignment and portfolio buy gates."""
from __future__ import annotations

import unittest

from execution_engine import ExecutionSettings, RiskGuard
from session_manager import align_deployable_cash, holdings_notional_usd


class DeployableCashTests(unittest.TestCase):
    def test_align_when_broker_cash_known(self) -> None:
        cash = align_deployable_cash(
            broker_cash_usd=12_500.0,
            capital_at_risk=100_000.0,
            holdings_notional_usd=80_000.0,
            fallback_cash_usd=100_000.0,
        )
        self.assertEqual(cash, 12_500.0)

    def test_align_infers_remaining_cap(self) -> None:
        cash = align_deployable_cash(
            broker_cash_usd=0.0,
            capital_at_risk=100_000.0,
            holdings_notional_usd=80_000.0,
            fallback_cash_usd=100_000.0,
        )
        self.assertEqual(cash, 20_000.0)

    def test_align_zero_when_over_deployed(self) -> None:
        cash = align_deployable_cash(
            broker_cash_usd=0.0,
            capital_at_risk=100_000.0,
            holdings_notional_usd=114_000.0,
            fallback_cash_usd=100_000.0,
        )
        self.assertEqual(cash, 0.0)

    def test_holdings_notional(self) -> None:
        states = {
            "TSLA": {"held_quantity": 10},
            "AAPL": {"held_quantity": 0},
        }
        total = holdings_notional_usd(states, ["TSLA", "AAPL"], {"TSLA": 400.0})
        self.assertEqual(total, 4_000.0)


class RiskGuardCapTests(unittest.TestCase):
    def test_blocks_buy_when_over_portfolio_cap(self) -> None:
        guard = RiskGuard(
            ExecutionSettings(
                max_daily_loss_usd=5_000,
                max_open_positions=5,
                max_ticker_exposure_usd=25_000,
                max_portfolio_usd=100_000,
                rth_buy_block_open_minutes=0,
                rth_buy_block_close_minutes=0,
                pending_order_stale_minutes=120,
                fill_inquiry_alert_cooldown_minutes=15,
                default_limit_buffer_bps=10,
                high_vol_limit_buffer_bps=15,
            )
        )
        states = {"TSLA": {"held_quantity": 0}}
        block = guard.check_buy_allowed(
            "TSLA",
            10,
            400.0,
            states,
            deployable_cash_usd=0.0,
            portfolio_deployed_usd=114_000.0,
        )
        self.assertIn("deployable cash", block or "")


if __name__ == "__main__":
    unittest.main()
