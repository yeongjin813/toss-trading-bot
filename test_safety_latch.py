"""Tests for automatic safety latch."""

from __future__ import annotations

import pytest

from safety_latch import SafetyLatchState, auto_buy_block_reason, update_from_issue_flags


def test_three_mismatches_engage_latch(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAFETY_LATCH_FILE", str(tmp_path / "safety_latch.json"))
    monkeypatch.setenv("SAFETY_LATCH_MISMATCH_COUNT", "3")
    for _ in range(2):
        update_from_issue_flags(holdings_mismatch=True)
        assert auto_buy_block_reason() is None
    _, newly = update_from_issue_flags(holdings_mismatch=True)
    assert newly
    assert auto_buy_block_reason() is not None


def test_mismatch_clear_resets_counter(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAFETY_LATCH_FILE", str(tmp_path / "safety_latch.json"))
    monkeypatch.setenv("SAFETY_LATCH_MISMATCH_COUNT", "3")
    update_from_issue_flags(holdings_mismatch=True)
    update_from_issue_flags(holdings_mismatch=True)
    update_from_issue_flags(holdings_mismatch=False)
    state = SafetyLatchState.load()
    assert state.counters["holdings_mismatch"] == 0
    assert not state.auto_block_new_buys
