"""Tests for rolling OOS walk-forward research helpers."""

from __future__ import annotations

from walk_forward_research import pick_train_winner, top3_variant_configs


def test_pick_train_winner_by_sharpe() -> None:
    train = {
        "legacy_equal": {"sharpe": 0.8, "cagr_pct": 10.0},
        "legacy_invvol": {"sharpe": 0.9, "cagr_pct": 9.0},
        "enhanced": {"sharpe": 0.7, "cagr_pct": 12.0},
    }
    assert pick_train_winner(train) == "legacy_invvol"


def test_pick_train_winner_tiebreak_cagr() -> None:
    train = {
        "legacy_equal": {"sharpe": 0.9, "cagr_pct": 11.0},
        "legacy_invvol": {"sharpe": 0.9, "cagr_pct": 9.0},
        "enhanced": {"sharpe": 0.5, "cagr_pct": 20.0},
    }
    assert pick_train_winner(train) == "legacy_equal"


def test_top3_variants_keep_legacy_production_shape() -> None:
    variants = top3_variant_configs()
    prod = variants["legacy_equal"]
    assert prod.ranking_mode == "legacy"
    assert prod.inverse_vol_weighting is False
    assert variants["legacy_invvol"].inverse_vol_weighting is True
    assert variants["enhanced"].ranking_mode == "enhanced"


def test_use_inverse_vol_weights_decoupled() -> None:
    from dataclasses import replace

    from momentum_ranker import MomentumRankSettings
    from momentum_selection import use_inverse_vol_weights

    legacy_inv = replace(
        MomentumRankSettings.from_env(),
        ranking_mode="legacy",
        inverse_vol_weighting=True,
    )
    assert use_inverse_vol_weights(legacy_inv) is True
    assert use_inverse_vol_weights(replace(legacy_inv, inverse_vol_weighting=False)) is False
