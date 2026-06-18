"""Tests for end-of-day report helpers."""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from daily_report import (
    compile_eod_metrics,
    format_eod_report_text,
    should_send_eod_report,
)


def test_should_send_eod_report_after_close(monkeypatch):
    monkeypatch.setenv("USE_DAILY_TELEGRAM_REPORT", "true")
    ny = datetime(2024, 6, 18, 16, 30, tzinfo=ZoneInfo("America/New_York"))
    states = {"_portfolio": {}}
    assert should_send_eod_report(ny, states) is True


def test_should_not_resend_same_day(monkeypatch):
    monkeypatch.setenv("USE_DAILY_TELEGRAM_REPORT", "true")
    ny = datetime(2024, 6, 18, 17, 0, tzinfo=ZoneInfo("America/New_York"))
    states = {"_portfolio": {"last_daily_report_date": "2024-06-18"}}
    assert should_send_eod_report(ny, states) is False


def test_compile_and_format_eod_metrics(tmp_path):
    log_path = tmp_path / "trade_log.csv"
    log_path.write_text(
        "timestamp,ticker,signal,qty,order_price,fill_price,status,reason,cash_after,held_qty\n"
        "2024-06-18T15:00:00,NVDA,BUY,1,900.0,900.0,FILLED,test,9000,1\n",
        encoding="utf-8",
    )
    states = {
        "_portfolio": {
            "day_start_equity_usd": 10000.0,
            "last_equity_usd": 10050.0,
            "active_trade_tickers": ["NVDA", "META"],
        },
        "NVDA": {"held_quantity": 1, "in_position": True},
    }
    metrics = compile_eod_metrics(
        states,
        ["NVDA", "META"],
        trade_log_path=str(log_path),
        available_cash=9100.0,
        now=datetime(2024, 6, 18, 16, 5, tzinfo=ZoneInfo("America/New_York")),
    )
    assert metrics["fills"] == 1
    assert metrics["open_positions"] == 1
    text = format_eod_report_text(metrics)
    assert "EOD Report" in text
    assert "NVDA" in text
