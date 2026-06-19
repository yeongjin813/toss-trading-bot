"""Tests for dual-strategy ownership registry."""

from __future__ import annotations

from strategy_ownership import (
    check_buy_collision,
    claim_ownership,
    load_ownership,
    reconcile_ownership,
    release_ownership,
)


def test_collision_blocks_cross_strategy_buy() -> None:
    states: dict[str, object] = {"_portfolio": {}}
    claim_ownership(states, "NVDA", "legacy")
    reason = check_buy_collision(states, "NVDA", "top3")
    assert reason is not None
    assert "legacy" in reason
    assert check_buy_collision(states, "NVDA", "legacy") is None


def test_reconcile_assigns_unowned_holdings() -> None:
    states = {
        "_portfolio": {},
        "AAPL": {"held_quantity": 5},
        "MSFT": {"held_quantity": 0},
    }
    reconcile_ownership(states, ["AAPL", "MSFT"], top3_active=["MSFT"])
    ownership = load_ownership(states)
    assert ownership["AAPL"] == "legacy"
    assert "MSFT" not in ownership


def test_release_clears_owner() -> None:
    states: dict[str, object] = {"_portfolio": {}}
    claim_ownership(states, "TSLA", "top3")
    release_ownership(states, "TSLA")
    assert load_ownership(states) == {}


def main() -> int:
    test_collision_blocks_cross_strategy_buy()
    test_reconcile_assigns_unowned_holdings()
    test_release_clears_owner()
    print("ALL STRATEGY OWNERSHIP TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
