"""Tests for scripts/ec2_healthcheck.py (no systemd required)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from heartbeat import HeartbeatState
from scripts.ec2_healthcheck import run_checks


def test_run_checks_ok_when_healthy(tmp_path: Path) -> None:
    (tmp_path / "project_metrics.log").write_bytes(b"x" * 1024)
    fresh = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    HeartbeatState(last_loop_at=fresh, last_broker_api_at=fresh).save(
        tmp_path / "heartbeat.json"
    )
    with patch("scripts.ec2_healthcheck._service_active", return_value=True):
        with patch("scripts.ec2_healthcheck._disk_used_pct", return_value=50.0):
            with patch("analytics.is_us_regular_market_hours", return_value=False):
                issues = run_checks(
                    bot_dir=tmp_path,
                    service="toss-bot",
                    disk_warn_pct=80.0,
                    log_warn_mb=400.0,
                )
    assert issues == []


def test_run_checks_flags_stale_heartbeat(tmp_path: Path) -> None:
    (tmp_path / "project_metrics.log").write_bytes(b"x" * 1024)
    stale = (
        datetime.now(timezone.utc) - timedelta(minutes=60)
    ).replace(microsecond=0).isoformat()
    HeartbeatState(last_loop_at=stale, last_broker_api_at=stale).save(
        tmp_path / "heartbeat.json"
    )
    with patch("scripts.ec2_healthcheck._service_active", return_value=True):
        with patch("scripts.ec2_healthcheck._disk_used_pct", return_value=50.0):
            with patch("analytics.is_us_regular_market_hours", return_value=True):
                issues = run_checks(
                    bot_dir=tmp_path,
                    service="toss-bot",
                    disk_warn_pct=80.0,
                    log_warn_mb=400.0,
                )
    assert any("Trading loop stale" in msg for _lvl, msg in issues)


def test_run_checks_flags_large_log(tmp_path: Path) -> None:
    (tmp_path / "project_metrics.log").write_bytes(b"x" * (1024 * 1024))
    fresh = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    HeartbeatState(last_loop_at=fresh, last_broker_api_at=fresh).save(
        tmp_path / "heartbeat.json"
    )
    with patch("scripts.ec2_healthcheck._service_active", return_value=True):
        with patch("scripts.ec2_healthcheck._disk_used_pct", return_value=50.0):
            with patch("analytics.is_us_regular_market_hours", return_value=False):
                issues = run_checks(
                    bot_dir=tmp_path,
                    service="toss-bot",
                    disk_warn_pct=80.0,
                    log_warn_mb=0.5,
                )
    assert any("project_metrics.log" in msg for _lvl, msg in issues)
