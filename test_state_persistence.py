"""Tests for atomic trading state persistence."""

from __future__ import annotations

import json
from pathlib import Path

from state_persistence import load_persisted_states, save_persisted_states


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "trading_state.json"
    payload = {"AAPL": {"held_quantity": 2}, "_portfolio": {"last_equity_usd": 1000.0}}
    save_persisted_states(payload, str(path))
    loaded = load_persisted_states(str(path))
    assert loaded == payload


def test_atomic_replace_leaves_valid_json_on_reread(tmp_path: Path) -> None:
    path = tmp_path / "trading_state.json"
    save_persisted_states({"v": 1}, str(path))
    save_persisted_states({"v": 2, "tickers": ["AAPL"]}, str(path))
    loaded = load_persisted_states(str(path))
    assert loaded["v"] == 2
    assert loaded["tickers"] == ["AAPL"]
    # File must be valid JSON (no partial write corruption)
    json.loads(path.read_text(encoding="utf-8"))


def test_load_missing_returns_empty_dict(tmp_path: Path) -> None:
    assert load_persisted_states(str(tmp_path / "missing.json")) == {}
