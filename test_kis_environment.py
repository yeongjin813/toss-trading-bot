"""Tests for KIS environment guards."""

from __future__ import annotations

import os

import pytest

from kis_environment import KIS_VTS_BASE_URL, load_kis_environment, validate_kis_live_guard


def test_default_is_vts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIS_BASE_URL", raising=False)
    monkeypatch.setenv("KIS_ENVIRONMENT", "vts")
    env = load_kis_environment()
    assert env.is_vts is True
    assert env.base_url == KIS_VTS_BASE_URL
    assert env.tr_id_us_buy.startswith("VTT")
    assert env.tr_id_us_sell == "VTTT1001U"


def test_live_sell_tr_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIS_ENVIRONMENT", "live")
    monkeypatch.setenv("KIS_LIVE_CONFIRMED", "true")
    monkeypatch.delenv("KIS_BASE_URL", raising=False)
    env = load_kis_environment()
    assert env.tr_id_us_sell == "TTTT1006U"


def test_vts_label_with_live_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIS_ENVIRONMENT", "vts")
    monkeypatch.setenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")
    with pytest.raises(RuntimeError, match="KIS_ENVIRONMENT=vts"):
        load_kis_environment()


def test_live_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIS_ENVIRONMENT", "live")
    monkeypatch.delenv("KIS_BASE_URL", raising=False)
    env = load_kis_environment()
    assert env.is_vts is False
    with pytest.raises(RuntimeError, match="KIS_LIVE_CONFIRMED"):
        validate_kis_live_guard(env, dry_run=False, capital_at_risk=100_000.0)


def test_live_confirmed_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIS_ENVIRONMENT", "live")
    monkeypatch.setenv("KIS_LIVE_CONFIRMED", "true")
    monkeypatch.delenv("KIS_BASE_URL", raising=False)
    env = load_kis_environment()
    validate_kis_live_guard(env, dry_run=False, capital_at_risk=100_000.0)


def main() -> int:
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
