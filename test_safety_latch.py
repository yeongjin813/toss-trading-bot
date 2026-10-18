"""Tests for automatic safety latch."""

from __future__ import annotations

import pytest

from safety_latch import SafetyLatchState, auto_buy_block_reason, update_from_issue_flags


def test_three_mismatches_alert_without_blocking(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paper default: mismatch is visible, but new buys stay allowed."""
    monkeypatch.setenv("SAFETY_LATCH_FILE", str(tmp_path / "safety_latch.json"))
    monkeypatch.setenv("SAFETY_LATCH_MISMATCH_COUNT", "3")
    monkeypatch.delenv("SAFETY_LATCH_BLOCK_BUYS", raising=False)
    for _ in range(2):
        _, newly = update_from_issue_flags(holdings_mismatch=True)
        assert newly == []
        assert auto_buy_block_reason() is None
    _, newly = update_from_issue_flags(holdings_mismatch=True)
    assert newly
    assert "alert only" in newly[0]
    assert auto_buy_block_reason() is None


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
    """EOD issues alert once and never block paper-trading buys."""
    monkeypatch.setenv("SAFETY_LATCH_FILE", str(tmp_path / "safety_latch.json"))
    monkeypatch.setenv("SAFETY_LATCH_EOD_MISSING_COUNT", "2")
    _, first = update_from_issue_flags(eod_missing=True)
    assert first == []
    state, alert = update_from_issue_flags(eod_missing=True)
    assert alert and "alert only" in alert[0]
    assert not state.auto_block_new_buys
    for _ in range(3):
        state, later = update_from_issue_flags(eod_missing=True)
        assert later == []
        assert not state.auto_block_new_buys
    assert state.counters["eod_missing"] == 5
    assert auto_buy_block_reason() is None


def test_telegram_failure_does_not_engage_latch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAFETY_LATCH_FILE", str(tmp_path / "safety_latch.json"))
    monkeypatch.setenv("SAFETY_LATCH_TELEGRAM_FAIL_COUNT", "2")
    update_from_issue_flags(telegram_failure=True)
    state, alert = update_from_issue_flags(telegram_failure=True)
    assert alert and "alert only" in alert[0]
    for _ in range(2):
        state, later = update_from_issue_flags(telegram_failure=True)
        assert later == []
        assert not state.auto_block_new_buys
    assert state.counters["telegram_failure"] == 4
    assert auto_buy_block_reason() is None
