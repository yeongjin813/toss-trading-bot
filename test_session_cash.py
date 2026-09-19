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

    def test_unrealized_pnl_vs_entry(self) -> None:
        from session_manager import unrealized_pnl_usd

        states = {
            "TSLA": {"held_quantity": 10, "entry_price": 400.0},
            "AAPL": {"held_quantity": 5, "entry_price": 200.0},
            "MSFT": {"held_quantity": 3},  # missing entry → 0
        }
        pnl = unrealized_pnl_usd(
            states,
            ["TSLA", "AAPL", "MSFT"],
            {"TSLA": 410.0, "AAPL": 190.0, "MSFT": 400.0},
        )
        # TSLA +100, AAPL -50 → +50
        self.assertEqual(pnl, 50.0)

    def test_vts_equity_not_flat_at_capital(self) -> None:
        """broker_cash=0 must not pin equity at CAPITAL when positions have entry."""
        from session_manager import unrealized_pnl_usd

        capital = 100_000.0
        states = {"UNH": {"held_quantity": 18, "entry_price": 422.03}}
        prices = {"UNH": 430.0}
        holdings = holdings_notional_usd(states, ["UNH"], prices)
        inferred_cash = align_deployable_cash(
            broker_cash_usd=0.0,
            capital_at_risk=capital,
            holdings_notional_usd=holdings,
            fallback_cash_usd=capital,
        )
        # Old bug: holdings + inferred_cash == capital
        self.assertAlmostEqual(holdings + inferred_cash, capital, places=2)
        equity = capital + unrealized_pnl_usd(states, ["UNH"], prices)
        self.assertGreater(equity, capital)
        self.assertAlmostEqual(equity, capital + 18 * (430.0 - 422.03), places=2)


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
                pending_order_cancel_minutes=45,
                max_consecutive_loss_days=3,
                max_positions_per_sector=2,
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
