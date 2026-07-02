"""
Automatic safety latch — blocks new BUYs after repeated operational anomalies.

Does not auto-liquidate. Persists to safety_latch.json (runtime file, not .env).
Clear with TRADING_PAUSED=true, fix root cause, delete safety_latch.json, restart.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LATCH_COUNTERS = (
    "holdings_mismatch",
    "broker_stale",
    "pending_order_stuck",
    "eod_missing",
    "telegram_failure",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def latch_file_path() -> Path:
    return Path(os.getenv("SAFETY_LATCH_FILE", "./safety_latch.json").strip())


@dataclass
class SafetyLatchState:
    auto_block_new_buys: bool = False
    block_reason: str | None = None
    latched_at: str | None = None
    counters: dict[str, int] = field(
        default_factory=lambda: {key: 0 for key in LATCH_COUNTERS}
    )

    @classmethod
    def load(cls, path: Path | None = None) -> SafetyLatchState:
        path = path or latch_file_path()
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return cls()
            counters = {key: 0 for key in LATCH_COUNTERS}
            raw_counters = raw.get("counters") or {}
            if isinstance(raw_counters, dict):
                for key in LATCH_COUNTERS:
                    counters[key] = int(raw_counters.get(key, 0) or 0)
            return cls(
                auto_block_new_buys=bool(raw.get("auto_block_new_buys")),
                block_reason=raw.get("block_reason"),
                latched_at=raw.get("latched_at"),
                counters=counters,
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or latch_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def latch_thresholds() -> dict[str, int]:
    return {
        "holdings_mismatch": _env_int("SAFETY_LATCH_MISMATCH_COUNT", 3),
        "broker_stale": _env_int("SAFETY_LATCH_BROKER_STALE_COUNT", 2),
        "pending_order_stuck": _env_int("SAFETY_LATCH_PENDING_STUCK_COUNT", 2),
        "eod_missing": _env_int("SAFETY_LATCH_EOD_MISSING_COUNT", 2),
        "telegram_failure": _env_int("SAFETY_LATCH_TELEGRAM_FAIL_COUNT", 3),
    }


def auto_buy_block_reason(state: SafetyLatchState | None = None) -> str | None:
    state = state or SafetyLatchState.load()
    if not state.auto_block_new_buys:
        return None
    detail = state.block_reason or "repeated operational anomalies"
    return f"safety latch engaged — {detail} (new BUY blocked; exits still allowed)"


def _engage_latch(state: SafetyLatchState, reason: str) -> bool:
    if state.auto_block_new_buys:
        return False
    state.auto_block_new_buys = True
    state.block_reason = reason
    state.latched_at = _utc_now_iso()
    return True


def record_anomaly(counter: str, *, active: bool) -> tuple[SafetyLatchState, bool]:
    """Increment or reset one counter. Returns (state, newly_latched)."""
    flags = {key: False for key in LATCH_COUNTERS}
    flags[counter] = active
    state, newly = update_from_issue_flags(**flags)  # type: ignore[arg-type]
    return state, bool(newly)


def update_from_issue_flags(
    *,
    holdings_mismatch: bool = False,
    broker_stale: bool = False,
    pending_order_stuck: bool = False,
    eod_missing: bool = False,
    telegram_failure: bool = False,
) -> tuple[SafetyLatchState, list[str]]:
    """Evaluate all latch counters from boolean issue flags in one atomic update."""
    flags = {
        "holdings_mismatch": holdings_mismatch,
        "broker_stale": broker_stale,
        "pending_order_stuck": pending_order_stuck,
        "eod_missing": eod_missing,
        "telegram_failure": telegram_failure,
    }
    state = SafetyLatchState.load()
    thresholds = latch_thresholds()
    newly_latched_reasons: list[str] = []
    for counter, active in flags.items():
        if active:
            state.counters[counter] = int(state.counters.get(counter, 0)) + 1
        else:
            state.counters[counter] = 0
        threshold = thresholds[counter]
        if state.counters[counter] >= threshold:
            if _engage_latch(
                state,
                f"{counter} reached {state.counters[counter]} consecutive hits "
                f"(threshold {threshold})",
            ):
                newly_latched_reasons.append(state.block_reason or counter)
    state.save()
    return state, newly_latched_reasons


def issue_flags_from_heartbeat(
    hb: Any,
    *,
    require_rth_activity: bool,
    now: datetime | None = None,
) -> dict[str, bool]:
    from heartbeat import _age_minutes, _env_int

    loop_stale = _env_int("HEARTBEAT_STALE_MINUTES", 15)
    broker_stale_min = _env_int("BROKER_API_STALE_MINUTES", 30)
    pending_timeout = _env_int("PENDING_ORDER_TIMEOUT_MINUTES", 120)

    flags = {
        "holdings_mismatch": bool(getattr(hb, "holdings_mismatch", False)),
        "broker_stale": False,
        "pending_order_stuck": False,
        "eod_missing": False,
        "telegram_failure": False,
    }

    if require_rth_activity:
        broker_age = _age_minutes(getattr(hb, "last_broker_api_at", None), now=now)
        if broker_age is not None and broker_age > broker_stale_min:
            flags["broker_stale"] = True

    if getattr(hb, "pending_order", False):
        pending_age = _age_minutes(getattr(hb, "pending_order_since", None), now=now)
        if pending_age is None or pending_age > pending_timeout:
            flags["pending_order_stuck"] = True

    eod_age = _age_minutes(getattr(hb, "last_eod_report_at", None), now=now)
    if eod_age is not None and eod_age > 24 * 60:
        flags["eod_missing"] = True

    return flags
