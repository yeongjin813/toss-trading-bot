"""Persist and retry failed KIS order submissions across watchlist cycles."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable


def _queue_path() -> str:
    return os.getenv("ORDER_RETRY_QUEUE_FILE", "./order_retry_queue.json")


def _max_attempts() -> int:
    return max(1, int(os.getenv("KIS_ORDER_MAX_RETRIES", "3")))


def _backoff_seconds() -> float:
    return float(os.getenv("KIS_ORDER_RETRY_BACKOFF_SECONDS", "2.0"))


@dataclass
class PendingOrderRetry:
    ticker: str
    signal: str
    quantity: int
    reference_price: float
    fail_key: str
    current_bar_date: str
    attempts: int = 0
    next_retry_at: str = ""
    last_error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PendingOrderRetry:
        return cls(
            ticker=str(payload["ticker"]),
            signal=str(payload["signal"]),
            quantity=int(payload["quantity"]),
            reference_price=float(payload["reference_price"]),
            fail_key=str(payload["fail_key"]),
            current_bar_date=str(payload["current_bar_date"]),
            attempts=int(payload.get("attempts", 0)),
            next_retry_at=str(payload.get("next_retry_at", "")),
            last_error=str(payload.get("last_error", "")),
            created_at=str(payload.get("created_at", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OrderRetryQueue:
    """File-backed queue processed at the start of each RTH watchlist cycle."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or _queue_path()
        self._items: list[PendingOrderRetry] = []
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            self._items = []
            return
        with open(self.path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, list):
            self._items = []
            return
        self._items = [PendingOrderRetry.from_dict(row) for row in raw if isinstance(row, dict)]

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump([item.to_dict() for item in self._items], handle, indent=2)

    def enqueue(self, item: PendingOrderRetry) -> None:
        item.attempts = max(item.attempts, 1)
        delay = _backoff_seconds() * (2 ** max(item.attempts - 1, 0))
        item.next_retry_at = (datetime.now() + timedelta(seconds=delay)).isoformat(
            timespec="seconds"
        )
        self._items = [row for row in self._items if row.fail_key != item.fail_key]
        self._items.append(item)
        self.save()
        print(
            f"[ORDER/QUEUE] {item.ticker} {item.signal} qty={item.quantity} "
            f"attempt={item.attempts}/{_max_attempts()} next={item.next_retry_at}"
        )

    def due_items(self, now: datetime | None = None) -> list[PendingOrderRetry]:
        current = now or datetime.now()
        due: list[PendingOrderRetry] = []
        for item in self._items:
            if not item.next_retry_at:
                due.append(item)
                continue
            try:
                retry_at = datetime.fromisoformat(item.next_retry_at)
            except ValueError:
                due.append(item)
                continue
            if retry_at <= current:
                due.append(item)
        return due

    def remove(self, item: PendingOrderRetry) -> None:
        self._items = [row for row in self._items if row.fail_key != item.fail_key]
        self.save()

    def process_due(
        self,
        executor: Callable[[PendingOrderRetry], dict[str, Any]],
        *,
        now: datetime | None = None,
    ) -> int:
        """Run executor for due items; re-queue or drop based on outcome."""
        processed = 0
        for item in list(self.due_items(now)):
            if item.attempts >= _max_attempts():
                print(
                    f"[ORDER/QUEUE/DROP] {item.ticker} {item.signal} - "
                    f"max attempts ({_max_attempts()}) exceeded"
                )
                self.remove(item)
                continue

            try:
                payload = executor(item)
            except Exception as exc:  # noqa: BLE001 — retry orchestration boundary
                item.attempts += 1
                item.last_error = str(exc)
                if item.attempts >= _max_attempts():
                    print(
                        f"[ORDER/QUEUE/DROP] {item.ticker} {item.signal} - "
                        f"{item.last_error}"
                    )
                    self.remove(item)
                else:
                    self.enqueue(item)
                continue

            if str(payload.get("rt_cd", "")) == "0" or payload.get("skipped"):
                print(f"[ORDER/RETRY-OK] {item.ticker} {item.signal} qty={item.quantity}")
                self.remove(item)
                processed += 1
            else:
                item.attempts += 1
                item.last_error = f"rt_cd={payload.get('rt_cd')}"
                if item.attempts >= _max_attempts():
                    self.remove(item)
                else:
                    self.enqueue(item)

        return processed
