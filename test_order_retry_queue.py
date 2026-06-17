"""Tests for persistent order retry queue."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta

from order_retry_queue import OrderRetryQueue, PendingOrderRetry


def test_enqueue_and_process_success() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "queue.json")
        queue = OrderRetryQueue(path)
        item = PendingOrderRetry(
            ticker="AAPL",
            signal="BUY",
            quantity=5,
            reference_price=100.0,
            fail_key="BUY:2026-06-11",
            current_bar_date="2026-06-11",
            attempts=1,
            next_retry_at=(datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds"),
        )
        queue._items = [item]
        queue.save()

        processed = queue.process_due(lambda row: {"rt_cd": "0"})
        assert processed == 1
        assert queue.due_items() == []


def test_drop_after_max_attempts(monkeypatch) -> None:
    monkeypatch.setenv("KIS_ORDER_MAX_RETRIES", "2")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "queue.json")
        queue = OrderRetryQueue(path)
        item = PendingOrderRetry(
            ticker="NVDA",
            signal="SELL",
            quantity=1,
            reference_price=200.0,
            fail_key="SELL:2026-06-11",
            current_bar_date="2026-06-11",
            attempts=2,
            next_retry_at=(datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds"),
        )
        queue._items = [item]
        queue.save()

        processed = queue.process_due(lambda row: (_ for _ in ()).throw(RuntimeError("fail")))
        assert processed == 0
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload == []


if __name__ == "__main__":
    test_enqueue_and_process_success()

    class _MonkeyPatch:
        def setenv(self, key, val):
            os.environ[key] = val

    test_drop_after_max_attempts(_MonkeyPatch())
    print("test_order_retry_queue.py - ALL PASS")
