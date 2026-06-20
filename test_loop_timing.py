"""Tests for adaptive loop cooldown."""

from __future__ import annotations

from loop_timing import effective_loop_cooldown_seconds, portfolio_needs_fast_poll


def test_fast_poll_when_holding() -> None:
    states = {"NVDA": {"held_quantity": 10}}
    assert portfolio_needs_fast_poll(states, ["NVDA", "AAPL"]) is True
    assert effective_loop_cooldown_seconds(states, ["NVDA"], flat_seconds=60, held_seconds=15) == 15


def test_flat_poll_when_empty() -> None:
    states = {"NVDA": {"held_quantity": 0, "pending_order": False}}
    assert portfolio_needs_fast_poll(states, ["NVDA"]) is False
    assert effective_loop_cooldown_seconds(states, ["NVDA"], flat_seconds=60, held_seconds=15) == 60


def test_fast_poll_on_pending_order() -> None:
    states = {"AAPL": {"held_quantity": 0, "pending_order": True, "open_order_id": "x"}}
    assert portfolio_needs_fast_poll(states, ["AAPL"]) is True


def main() -> int:
    test_fast_poll_when_holding()
    test_flat_poll_when_empty()
    test_fast_poll_on_pending_order()
    print("ALL LOOP TIMING TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
