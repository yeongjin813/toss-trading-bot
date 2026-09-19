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


def test_eod_missing_does_not_engage_latch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EOD/Telegram issues stay soft — never block paper-trading buys."""
    monkeypatch.setenv("SAFETY_LATCH_FILE", str(tmp_path / "safety_latch.json"))
    monkeypatch.setenv("SAFETY_LATCH_EOD_MISSING_COUNT", "2")
    for _ in range(5):
        state, newly = update_from_issue_flags(eod_missing=True)
        assert newly == []
        assert not state.auto_block_new_buys
    assert state.counters["eod_missing"] == 5
    assert auto_buy_block_reason() is None


def test_telegram_failure_does_not_engage_latch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAFETY_LATCH_FILE", str(tmp_path / "safety_latch.json"))
    monkeypatch.setenv("SAFETY_LATCH_TELEGRAM_FAIL_COUNT", "2")
    for _ in range(4):
        state, newly = update_from_issue_flags(telegram_failure=True)
        assert newly == []
        assert not state.auto_block_new_buys
    assert state.counters["telegram_failure"] == 4
