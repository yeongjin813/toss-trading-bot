"""
Trim planner when marked holdings exceed CAPITAL_AT_RISK (duplicate-buy recovery).

Sells from the largest positions first until marked notional is at or below target.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from session_manager import holdings_notional_usd


@dataclass(frozen=True)
class TrimOrder:
    ticker: str
    shares: int
    reference_price: float
    notional_usd: float


def plan_overdeployment_trims(
    states: Mapping[str, Any],
    watchlist: list[str],
    prices: Mapping[str, float],
    *,
    capital_at_risk: float,
    target_pct: float = 0.98,
) -> tuple[list[TrimOrder], float, float]:
    """
    Build SELL trim orders when portfolio marks exceed capital cap.

    Returns (orders, marked_notional, target_notional).
    """
    marked = holdings_notional_usd(states, watchlist, prices)
    target = max(0.0, capital_at_risk * target_pct)
    if marked <= target * 1.005:
        return [], marked, target

    excess = marked - target
    positions: list[tuple[str, int, float, float]] = []
    for ticker in watchlist:
        payload = states.get(ticker, {})
        qty = 0
        if isinstance(payload, dict):
            qty = int(payload.get("held_quantity", 0) or 0)
        price = float(prices.get(ticker, 0.0) or 0.0)
        if qty > 0 and price > 0:
            positions.append((ticker, qty, price, qty * price))

    positions.sort(key=lambda row: row[3], reverse=True)

    trims: list[TrimOrder] = []
    remaining = excess
    for ticker, qty, price, _notional in positions:
        if remaining <= 0:
            break
        sell_notional = min(remaining, qty * price)
        shares = min(qty, max(1, int((sell_notional + price - 1e-9) // price)))
        if shares <= 0:
            continue
        notional = shares * price
        trims.append(
            TrimOrder(
                ticker=ticker,
                shares=shares,
                reference_price=price,
                notional_usd=notional,
            )
        )
        remaining -= notional

    return trims, marked, target


def format_trim_plan_text(
    trims: list[TrimOrder],
    *,
    marked: float,
    target: float,
    capital_at_risk: float,
) -> str:
    if not trims:
        return ""
    lines = [
        f"[TRIM] Marked ${marked:,.2f} > target ${target:,.2f} "
        f"(cap ${capital_at_risk:,.2f})",
    ]
    for order in trims:
        lines.append(
            f"  SELL {order.shares} {order.ticker} @ ~${order.reference_price:.2f} "
            f"(~${order.notional_usd:,.2f})"
        )
    total = sum(o.notional_usd for o in trims)
    lines.append(f"  Planned trim ~${total:,.2f}")
    return "\n".join(lines)
