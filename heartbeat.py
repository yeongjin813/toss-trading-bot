"""
Runtime heartbeat timestamps for silent-failure detection during unattended operation.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _age_minutes(value: str | None, *, now: datetime | None = None) -> float | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    ref = now or datetime.now(timezone.utc)
    return max(0.0, (ref - parsed).total_seconds() / 60.0)


def heartbeat_file_path() -> Path:
    raw = os.getenv("HEARTBEAT_FILE", "./heartbeat.json").strip()
    return Path(raw)


@dataclass
class HeartbeatState:
    last_loop_at: str | None = None
    last_broker_api_at: str | None = None
    last_reconcile_at: str | None = None
    last_telegram_at: str | None = None
    last_eod_report_at: str | None = None
    pending_order: bool = False
    pending_order_ticker: str | None = None
    pending_order_since: str | None = None
    holdings_mismatch: bool = False
    holdings_mismatch_detail: str | None = None
    active_kill_switches: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> HeartbeatState:
        path = path or heartbeat_file_path()
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return cls()
            return cls(
                last_loop_at=raw.get("last_loop_at"),
                last_broker_api_at=raw.get("last_broker_api_at"),
                last_reconcile_at=raw.get("last_reconcile_at"),
                last_telegram_at=raw.get("last_telegram_at"),
                last_eod_report_at=raw.get("last_eod_report_at"),
                pending_order=bool(raw.get("pending_order")),
                pending_order_ticker=raw.get("pending_order_ticker"),
                pending_order_since=raw.get("pending_order_since"),
                holdings_mismatch=bool(raw.get("holdings_mismatch")),
                holdings_mismatch_detail=raw.get("holdings_mismatch_detail"),
                active_kill_switches=list(raw.get("active_kill_switches") or []),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or heartbeat_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def touch_loop(self) -> None:
        self.last_loop_at = _utc_now_iso()

    def touch_broker_api(self) -> None:
        self.last_broker_api_at = _utc_now_iso()

    def touch_reconcile(self) -> None:
        self.last_reconcile_at = _utc_now_iso()

    def touch_telegram(self) -> None:
        self.last_telegram_at = _utc_now_iso()

    def touch_eod_report(self) -> None:
        self.last_eod_report_at = _utc_now_iso()


def update_pending_order_snapshot(
    states: dict[str, Any],
    watchlist: list[str],
    hb: HeartbeatState | None = None,
) -> HeartbeatState:
    hb = hb or HeartbeatState.load()
    pending_ticker: str | None = None
    pending_since: str | None = None
    for ticker in watchlist:
        payload = states.get(ticker, {})
        if not isinstance(payload, dict):
            continue
        if payload.get("pending_order"):
            pending_ticker = ticker
            pending_since = payload.get("open_order_submitted_at") or hb.pending_order_since
            break
    hb.pending_order = pending_ticker is not None
    hb.pending_order_ticker = pending_ticker
    hb.pending_order_since = pending_since
    return hb


def update_holdings_mismatch(
    states: dict[str, Any],
    watchlist: list[str],
    hb: HeartbeatState | None = None,
) -> HeartbeatState:
    hb = hb or HeartbeatState.load()
    portfolio = states.get("_portfolio", {})
    broker = portfolio.get("broker_holdings") or {}
    mismatches: list[str] = []
    for ticker in watchlist:
        payload = states.get(ticker, {})
        local = int(payload.get("held_quantity", 0) or 0) if isinstance(payload, dict) else 0
        broker_qty = int(broker.get(ticker, broker.get(ticker.upper(), 0)) or 0)
        if local != broker_qty:
            mismatches.append(f"{ticker}: local={local} broker={broker_qty}")
    hb.holdings_mismatch = bool(mismatches)
    hb.holdings_mismatch_detail = "; ".join(mismatches[:5]) if mismatches else None
    return hb


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def check_heartbeat_issues(
    hb: HeartbeatState | None = None,
    *,
    now: datetime | None = None,
    require_rth_activity: bool = True,
) -> list[tuple[str, str]]:
    """Return (level, message) issues for external healthcheck."""
    hb = hb or HeartbeatState.load()
    issues: list[tuple[str, str]] = []
    loop_stale = _env_int("HEARTBEAT_STALE_MINUTES", 15)
    broker_stale = _env_int("BROKER_API_STALE_MINUTES", 30)
    pending_timeout = _env_int("PENDING_ORDER_TIMEOUT_MINUTES", 120)

    if require_rth_activity:
        loop_age = _age_minutes(hb.last_loop_at, now=now)
        if loop_age is None:
            issues.append(("WARNING", "No trading loop heartbeat recorded yet"))
        elif loop_age > loop_stale:
            issues.append(
                (
                    "CRITICAL",
                    f"Trading loop stale {loop_age:.0f}m (threshold {loop_stale}m)",
                )
            )

        broker_age = _age_minutes(hb.last_broker_api_at, now=now)
        if broker_age is None:
            issues.append(("WARNING", "No broker API heartbeat recorded yet"))
        elif broker_age > broker_stale:
            issues.append(
                (
                    "CRITICAL",
                    f"Broker API stale {broker_age:.0f}m (threshold {broker_stale}m)",
                )
            )

    if hb.pending_order:
        pending_age = _age_minutes(hb.pending_order_since, now=now)
        ticker = hb.pending_order_ticker or "?"
        if pending_age is not None and pending_age > pending_timeout:
            issues.append(
                (
                    "WARNING",
                    f"Pending order stuck {pending_age:.0f}m on {ticker} "
                    f"(threshold {pending_timeout}m)",
                )
            )
        elif pending_age is None:
            issues.append(("WARNING", f"Pending order open on {ticker} (no submit timestamp)"))

    if hb.holdings_mismatch:
        detail = hb.holdings_mismatch_detail or "local vs broker qty differ"
        issues.append(("WARNING", f"Holdings mismatch: {detail}"))

    if hb.active_kill_switches:
        issues.append(
            (
                "INFO",
                "Kill switches active: " + ", ".join(hb.active_kill_switches),
            )
        )

    eod_age = _age_minutes(hb.last_eod_report_at, now=now)
    if require_rth_activity and hb.last_eod_report_at is None:
        issues.append(("INFO", "No EOD report heartbeat recorded yet"))
    elif (
        require_rth_activity
        and eod_age is not None
        and eod_age > 24 * 60
    ):
        issues.append(
            (
                "WARNING",
                f"EOD report heartbeat older than 24h ({eod_age / 60:.1f}h)",
            )
        )

    return issues
