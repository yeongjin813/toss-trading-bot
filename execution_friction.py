"""Execution friction helpers for backtest parity with live costs."""

from __future__ import annotations


def slippage_fraction(slippage_bps: float) -> float:
    return max(0.0, float(slippage_bps)) / 10_000.0


def fill_price(side: str, reference: float, *, slippage_bps: float) -> float:
    """BUY pays more; SELL receives less."""
    slip = slippage_fraction(slippage_bps)
    if side.upper() == "BUY":
        return reference * (1.0 + slip)
    return reference * (1.0 - slip)


def round_trip_cost_pct(commission_rate: float, slippage_bps: float) -> float:
    """Approximate one buy+sell round trip as % of notional."""
    slip = slippage_fraction(slippage_bps)
    return 2.0 * (commission_rate + slip) * 100.0
