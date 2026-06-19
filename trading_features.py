"""Shared live/backtest feature flags and regime helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass

import pandas as pd

from analytics import (
    MarketRegime,
    build_market_regime_lookup,
    calculate_atr,
    resolve_market_regime,
    resolve_spy_market_bullish,
    volatility_adjusted_risk_fraction,
)
from config import StrategyConfigMapper
from market_registry import BENCHMARK_CONFIRM_SMA_PERIOD, BENCHMARK_SMA_PERIOD


def _flag(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TradingFeatureFlags:
    use_vol_adjusted_risk: bool = True
    vol_target_pct: float = 0.015
    use_regime_golden_cross: bool = True
    regime_cautious_max_positions: int = 2
    use_scale_in: bool = True
    use_scale_out: bool = True
    use_weekly_trend_filter: bool = True
    weekly_trend_sma_period: int = 20
    use_52w_high_filter: bool = True
    near_52w_high_pct: float = 0.05

    @classmethod
    def from_env(cls) -> TradingFeatureFlags:
        return cls(
            use_vol_adjusted_risk=_flag("USE_VOL_ADJUSTED_RISK", "true"),
            vol_target_pct=float(os.getenv("VOL_TARGET_PCT", "0.015")),
            use_regime_golden_cross=_flag("USE_REGIME_GOLDEN_CROSS", "true"),
            regime_cautious_max_positions=int(
                os.getenv("REGIME_CAUTIOUS_MAX_POSITIONS", "2")
            ),
            use_scale_in=_flag("USE_SCALE_IN", "true"),
            use_scale_out=_flag("USE_SCALE_OUT", "true"),
            use_weekly_trend_filter=_flag("USE_WEEKLY_TREND_FILTER", "true"),
            weekly_trend_sma_period=int(os.getenv("WEEKLY_TREND_SMA_PERIOD", "20")),
            use_52w_high_filter=_flag("USE_52W_HIGH_FILTER", "true"),
            near_52w_high_pct=float(os.getenv("NEAR_52W_HIGH_PCT", "0.05")),
        )

    def entry_filter_settings(self) -> dict[str, bool | float | int]:
        return {
            "weekly_trend": self.use_weekly_trend_filter,
            "weekly_sma_period": self.weekly_trend_sma_period,
            "near_52w_high": self.use_52w_high_filter,
            "near_52w_high_pct": self.near_52w_high_pct,
        }


def build_regime_lookup(
    spy_df: pd.DataFrame | None,
    qqq_df: pd.DataFrame | None,
    *,
    features: TradingFeatureFlags | None = None,
) -> dict[str, MarketRegime] | None:
    flags = features or TradingFeatureFlags.from_env()
    if spy_df is None or spy_df.empty:
        return None
    if (
        StrategyConfigMapper.use_qqq_regime_filter()
        and qqq_df is not None
        and not qqq_df.empty
    ):
        return build_market_regime_lookup(
            spy_df,
            qqq_df,
            sma_period=BENCHMARK_SMA_PERIOD,
            confirm_sma_period=BENCHMARK_CONFIRM_SMA_PERIOD,
            require_golden_cross=flags.use_regime_golden_cross,
            cautious_max_positions=flags.regime_cautious_max_positions,
        )
    return None


def resolve_bar_regime(
    bar_date: str,
    *,
    regime_lookup: dict[str, MarketRegime] | None,
    spy_lookup: dict[str, bool] | None,
) -> MarketRegime:
    if regime_lookup:
        return resolve_market_regime(regime_lookup, bar_date)
    bullish = resolve_spy_market_bullish(spy_lookup, bar_date)
    if bullish:
        return MarketRegime(
            allow_new_buys=True,
            position_size_multiplier=1.0,
            spy_bullish=True,
            qqq_bullish=True,
            label="normal",
            atr_stop_multiplier=1.0,
        )
    return MarketRegime(
        allow_new_buys=False,
        position_size_multiplier=0.0,
        spy_bullish=False,
        qqq_bullish=False,
        label="risk_off",
        max_open_positions=0,
        atr_stop_multiplier=0.7,
    )


def build_spy_atr_pct_lookup(
    spy_df: pd.DataFrame,
    *,
    atr_period: int = 14,
) -> dict[str, float]:
    frame = spy_df.copy()
    if "Date" in frame.columns:
        frame["Date"] = pd.to_datetime(frame["Date"])
        frame = frame.sort_values("Date")
    atr = calculate_atr(frame, atr_period)
    closes = frame["Close"].astype(float)
    lookup: dict[str, float] = {}
    dates = frame["Date"] if "Date" in frame.columns else frame.index
    for idx, (dt, close) in enumerate(zip(dates, closes, strict=False)):
        if idx >= len(atr) or pd.isna(atr.iloc[idx]) or close <= 0:
            continue
        bar_date = pd.Timestamp(dt).strftime("%Y-%m-%d")
        lookup[bar_date] = float(atr.iloc[idx]) / float(close)
    return lookup


def effective_risk_per_trade(
    base_risk: float,
    bar_date: str,
    *,
    spy_atr_lookup: dict[str, float] | None,
    features: TradingFeatureFlags | None = None,
) -> float:
    flags = features or TradingFeatureFlags.from_env()
    if not flags.use_vol_adjusted_risk or not spy_atr_lookup:
        return base_risk
    atr_pct = spy_atr_lookup.get(bar_date)
    if not atr_pct or atr_pct <= 0:
        return base_risk
    return volatility_adjusted_risk_fraction(
        base_risk,
        atr_pct,
        target_vol_pct=flags.vol_target_pct,
    )
