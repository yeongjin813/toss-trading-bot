"""Tests for four-phase deployment configuration."""

from __future__ import annotations

import os
from unittest import mock

from deployment_config import DeploymentConfig, scaled_capital


def test_phase1_legacy_defaults():
    with mock.patch.dict(os.environ, {}, clear=True):
        cfg = DeploymentConfig.from_env()
        assert cfg.phase == 1
        assert cfg.strategy_mode == "legacy"
        assert not cfg.is_dual
        assert not cfg.top3_shadow_active
        assert cfg.legacy_capital_fraction() == 1.0
        assert cfg.legacy_momentum_rank_enabled(False) is False


def test_phase3_dual_shadow():
    env = {
        "DEPLOYMENT_PHASE": "3",
        "STRATEGY_MODE": "dual",
        "TOP3_DRY_RUN_ENABLED": "true",
        "LEGACY_CAPITAL_PCT": "60",
        "TOP3_CAPITAL_PCT": "40",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = DeploymentConfig.from_env()
        assert cfg.is_dual
        assert cfg.top3_shadow_active
        assert not cfg.top3_live_orders
        assert cfg.legacy_capital_fraction() == 0.6
        assert cfg.top3_capital_fraction() == 0.4
        assert cfg.legacy_momentum_rank_enabled(True) is False


def test_phase4_live_split():
    env = {
        "DEPLOYMENT_PHASE": "4",
        "STRATEGY_MODE": "dual",
        "LEGACY_CAPITAL_PCT": "60",
        "TOP3_CAPITAL_PCT": "40",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = DeploymentConfig.from_env()
        assert cfg.top3_live_orders
        assert cfg.top3_shadow_active


def test_scaled_capital():
    assert scaled_capital(10_000, 0.6) == 6_000
