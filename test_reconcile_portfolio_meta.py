"""Reconciliation must preserve _portfolio metadata (trim dates, Top3, etc.)."""

from __future__ import annotations

from session_manager import PortfolioReconciliationEngine


def test_reconcile_preserves_portfolio_metadata() -> None:
    states = {
        "TSM": {"held_quantity": 73, "in_position": True},
        "_portfolio": {
            "last_overdeployment_trim_date": "2026-06-22",
            "active_trade_tickers": ["TSM", "NVDA", "AMD"],
            "last_daily_report_date": "2026-06-21",
        },
    }

    class FakeClient:
        def fetch_overseas_present_balance(self, **_kwargs):
            return {
                "output1": [{"pdno": "TSM", "ovrs_stck_tot_qty": "73"}],
                "output2": [{"crcy_cd": "USD", "frcr_dncl_amt_2": "5000", "frcr_drwg_psbl_amt_1": "5000"}],
            }

    engine = PortfolioReconciliationEngine(["TSM"])
    states, _ledger, report = engine.reconcile(
        FakeClient(),
        states,
        fallback_cash=100_000.0,
        capital_at_risk=100_000.0,
        mark_prices={"TSM": 460.0},
    )

    assert report.reconciled is True
    portfolio = states["_portfolio"]
    assert portfolio["last_overdeployment_trim_date"] == "2026-06-22"
    assert portfolio["active_trade_tickers"] == ["TSM", "NVDA", "AMD"]
    assert portfolio["last_daily_report_date"] == "2026-06-21"
    assert portfolio["broker_holdings"] == {"TSM": 73}
    assert portfolio["broker_cash_usd"] == 5000.0
