"""EOD report should not mark sent when Telegram dispatch fails."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import main as main_module


def test_eod_not_marked_when_telegram_fails(monkeypatch):
    monkeypatch.setenv("USE_DAILY_TELEGRAM_REPORT", "true")
    monkeypatch.setenv("USE_TELEGRAM_ALERTS", "true")

    states = {"_portfolio": {}}
    ledger = SimpleNamespace(available_cash_usd=1000.0)
    ny = datetime(2024, 6, 18, 16, 5, tzinfo=ZoneInfo("America/New_York"))

    monkeypatch.setattr(main_module, "compile_eod_metrics", lambda *a, **k: {"date": "2024-06-18", "equity": 1000.0})
    monkeypatch.setattr(main_module, "format_eod_report_text", lambda metrics: "report")
    monkeypatch.setattr(main_module, "_telegram_enabled", lambda: True)
    monkeypatch.setattr(main_module, "_run_telegram", lambda coro: False)
    monkeypatch.setattr(main_module, "mark_eod_report_sent", lambda s, **k: s["_portfolio"].update({"last_daily_report_date": "2024-06-18"}))
    monkeypatch.setattr(main_module, "record_daily_equity_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "save_persisted_states", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "_persist_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "_maybe_send_weekly_report", lambda *a, **k: None)

    sent = main_module._maybe_send_eod_report(states, ledger, now=ny)
    assert sent is False
    assert states["_portfolio"].get("last_daily_report_date") is None


def test_eod_marked_when_telegram_succeeds(monkeypatch):
    monkeypatch.setenv("USE_DAILY_TELEGRAM_REPORT", "true")
    monkeypatch.setenv("USE_TELEGRAM_ALERTS", "true")

    states = {"_portfolio": {}}
    ledger = SimpleNamespace(available_cash_usd=1000.0)
    ny = datetime(2024, 6, 18, 16, 5, tzinfo=ZoneInfo("America/New_York"))

    monkeypatch.setattr(main_module, "compile_eod_metrics", lambda *a, **k: {"date": "2024-06-18", "equity": 1000.0})
    monkeypatch.setattr(main_module, "format_eod_report_text", lambda metrics: "report")
    monkeypatch.setattr(main_module, "_telegram_enabled", lambda: True)
    monkeypatch.setattr(main_module, "_run_telegram", lambda coro: True)
    monkeypatch.setattr(main_module, "mark_eod_report_sent", lambda s, **k: s["_portfolio"].update({"last_daily_report_date": "2024-06-18"}))
    monkeypatch.setattr(main_module, "record_daily_equity_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "save_persisted_states", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "_persist_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "_maybe_send_weekly_report", lambda *a, **k: None)

    sent = main_module._maybe_send_eod_report(states, ledger, now=ny)
    assert sent is True
    assert states["_portfolio"]["last_daily_report_date"] == "2024-06-18"
