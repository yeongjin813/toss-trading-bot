"""Tests for heartbeat stale detection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from heartbeat import HeartbeatState, check_heartbeat_issues


def _iso_minutes_ago(minutes: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).replace(microsecond=0).isoformat()


def test_fresh_heartbeat_has_no_critical_issues() -> None:
    hb = HeartbeatState(
        last_loop_at=_iso_minutes_ago(1),
        last_broker_api_at=_iso_minutes_ago(2),
    )
    critical = [lvl for lvl, _msg in check_heartbeat_issues(hb, require_rth_activity=True) if lvl == "CRITICAL"]
    assert critical == []


def test_stale_loop_raises_critical() -> None:
    hb = HeartbeatState(
        last_loop_at=_iso_minutes_ago(30),
        last_broker_api_at=_iso_minutes_ago(1),
    )
    issues = check_heartbeat_issues(hb, require_rth_activity=True)
    assert any("Trading loop stale" in msg for _lvl, msg in issues)


def test_pending_order_stuck_warning() -> None:
    hb = HeartbeatState(
        last_loop_at=_iso_minutes_ago(1),
        last_broker_api_at=_iso_minutes_ago(1),
        pending_order=True,
        pending_order_ticker="AAPL",
        pending_order_since=_iso_minutes_ago(180),
    )
    issues = check_heartbeat_issues(hb, require_rth_activity=True)
    assert any("Pending order stuck" in msg for _lvl, msg in issues)


def test_holdings_mismatch_warning() -> None:
    hb = HeartbeatState(
        last_loop_at=_iso_minutes_ago(1),
        last_broker_api_at=_iso_minutes_ago(1),
        holdings_mismatch=True,
        holdings_mismatch_detail="AAPL: local=1 broker=0",
    )
    issues = check_heartbeat_issues(hb, require_rth_activity=True)
    assert any("Holdings mismatch" in msg for _lvl, msg in issues)
