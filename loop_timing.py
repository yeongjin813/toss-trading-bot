"""
Adaptive RTH loop cooldown — faster polling while positions or pending orders exist.
"""

from __future__ import annotations

from typing import Any, Mapping


def portfolio_needs_fast_poll(
    states: Mapping[str, Any],
    watchlist: list[str],
) -> bool:
    """True when any watchlist name is held or has an open/pending order."""
    for ticker in watchlist:
        payload = states.get(ticker, {})
        if not isinstance(payload, dict):
            continue
        if int(payload.get("held_quantity", 0) or 0) > 0:
            return True
        if payload.get("pending_order") or payload.get("open_order_id"):
            return True
    return False


def effective_loop_cooldown_seconds(
    states: Mapping[str, Any],
    watchlist: list[str],
    *,
    flat_seconds: int,
    held_seconds: int,
) -> int:
    """Shorter sleep when exposed to market risk (holdings / pending orders)."""
    flat = max(5, int(flat_seconds))
    held = max(5, min(int(held_seconds), flat))
    if portfolio_needs_fast_poll(states, watchlist):
        return held
    return flat
