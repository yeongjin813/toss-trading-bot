"""
Mutual-exclusion registry for dual-strategy deployment (Legacy vs Top3).

KIS does not tag orders by strategy; broker positions are shared. This module
prevents both strategies from opening new positions in the same ticker.
"""

from __future__ import annotations

from typing import Any, Literal

StrategyName = Literal["legacy", "top3"]

OWNERSHIP_KEY = "strategy_ownership"


def load_ownership(states: dict[str, Any]) -> dict[str, str]:
    portfolio = states.get("_portfolio", {})
    raw = portfolio.get(OWNERSHIP_KEY) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).upper(): str(v) for k, v in raw.items()}


def save_ownership(states: dict[str, Any], ownership: dict[str, str]) -> None:
    portfolio = states.setdefault("_portfolio", {})
    portfolio[OWNERSHIP_KEY] = {k.upper(): v for k, v in ownership.items()}


def claim_ownership(
    states: dict[str, Any],
    ticker: str,
    strategy: StrategyName,
) -> None:
    ownership = load_ownership(states)
    ownership[ticker.upper()] = strategy
    save_ownership(states, ownership)


def release_ownership(states: dict[str, Any], ticker: str) -> None:
    ownership = load_ownership(states)
    ownership.pop(ticker.upper(), None)
    save_ownership(states, ownership)


def check_buy_collision(
    states: dict[str, Any],
    ticker: str,
    strategy: StrategyName,
) -> str | None:
    """Return block reason when another strategy owns this ticker."""
    owner = load_ownership(states).get(ticker.upper())
    if owner and owner != strategy:
        return f"ticker owned by {owner} strategy"
    return None


def reconcile_ownership(
    states: dict[str, Any],
    watchlist: list[str],
    *,
    top3_active: list[str] | None = None,
) -> None:
    """
    Sync ownership with held quantities.

    Held tickers without an owner are assigned to Top3 when in active set,
    otherwise Legacy. Flat positions release ownership.
    """
    ownership = load_ownership(states)
    top3_set = {t.upper() for t in (top3_active or [])}

    for ticker in watchlist:
        key = ticker.upper()
        payload = states.get(ticker, {})
        held = int(payload.get("held_quantity", 0) or 0) if isinstance(payload, dict) else 0
        if held <= 0:
            ownership.pop(key, None)
            continue
        if key not in ownership:
            ownership[key] = "top3" if key in top3_set else "legacy"

    save_ownership(states, ownership)
