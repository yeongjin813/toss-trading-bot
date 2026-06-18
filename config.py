"""
Ticker-specific strategy configuration isolation.

Execution parameters resolve exclusively through StrategyConfigMapper.for_ticker().
Global .env SMA/RSI/ATR overrides are not consulted for signal generation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import ClassVar, Literal, Mapping

from market_registry import BENCHMARK_SMA_PERIOD, BENCHMARK_TICKER

EntryMode = Literal["dual", "breakout", "crossover"]

# trend_exit_days <= 0 disables 50MA consecutive-day exit
TREND_EXIT_DISABLED = 0


@dataclass(frozen=True)
class TickerConfig:
    """
    Regime-scoped parameter bundle keyed to volatility/trend personality.

    entry_mode:
      dual      — breakout OR pullback OR golden cross
      breakout  — 20-day high breakout (trend leaders)
      crossover — legacy golden cross only
    """

    sma_period: int
    rsi_buy_threshold: float
    volume_threshold: float
    atr_multiplier: float
    use_trend_filter: bool
    entry_mode: EntryMode = "breakout"
    breakout_lookback: int = 20
    stop_loss_pct: float = 0.08
    hard_stop_atr_mult: float = 2.0
    trend_exit_days: int = 5
    profit_trail_activation_pct: float = 0.15
    profit_trail_drawdown_pct: float = 0.15
    min_hold_days: int = 5
    skip_trend_exit_when_ranked: bool = False
    pullback_rsi_low: float = 40.0
    pullback_rsi_high: float = 60.0
    pullback_ma_tolerance_pct: float = 3.0


@dataclass(frozen=True)
class StrategyConfig:
    """Fully resolved, ticker-bound configuration for live and backtest engines."""

    ticker: str
    sma_period: int
    rsi_buy_threshold: float
    volume_threshold: float
    atr_multiplier: float
    use_trend_filter: bool
    entry_mode: EntryMode
    breakout_lookback: int
    stop_loss_pct: float
    hard_stop_atr_mult: float
    trend_exit_days: int
    profit_trail_activation_pct: float
    profit_trail_drawdown_pct: float
    min_hold_days: int
    skip_trend_exit_when_ranked: bool
    pullback_rsi_low: float
    pullback_rsi_high: float
    pullback_ma_tolerance_pct: float
    rsi_period: int = 14
    atr_period: int = 14
    volume_sma_period: int = 20
    sma_long_period: int = 50
    rsi_exit_threshold: float = 50.0
    use_trailing_stop: bool = True

    @classmethod
    def from_ticker_config(cls, ticker: str, regime: TickerConfig) -> StrategyConfig:
        return cls(
            ticker=ticker.upper(),
            sma_period=regime.sma_period,
            rsi_buy_threshold=regime.rsi_buy_threshold,
            volume_threshold=regime.volume_threshold,
            atr_multiplier=regime.atr_multiplier,
            use_trend_filter=regime.use_trend_filter,
            entry_mode=regime.entry_mode,
            breakout_lookback=regime.breakout_lookback,
            stop_loss_pct=regime.stop_loss_pct,
            hard_stop_atr_mult=regime.hard_stop_atr_mult,
            trend_exit_days=regime.trend_exit_days,
            profit_trail_activation_pct=regime.profit_trail_activation_pct,
            profit_trail_drawdown_pct=regime.profit_trail_drawdown_pct,
            min_hold_days=regime.min_hold_days,
            skip_trend_exit_when_ranked=regime.skip_trend_exit_when_ranked,
            pullback_rsi_low=regime.pullback_rsi_low,
            pullback_rsi_high=regime.pullback_rsi_high,
            pullback_ma_tolerance_pct=regime.pullback_ma_tolerance_pct,
        )


class StrategyConfigMapper:
    """
    Parameter isolation matrix: ticker symbol -> StrategyConfig.

    Regimes (Phase 10 — trend hold, fewer whipsaws):
      MEGA_CAP   — breakout entry, 50MA exit after 5 days, moderate trail
      HIGH_BETA  — breakout, no 50MA exit while ranked, wider trail
      MOMENTUM   — breakout, hold winners longer (25% arm / 18% trail)
      DEFAULT    — breakout baseline for TSM/SHOP/UBER
    """

    SMA_LONG_PERIOD: ClassVar[int] = 50

    _MEGA: ClassVar[TickerConfig] = TickerConfig(
        sma_period=20,
        rsi_buy_threshold=45.0,
        volume_threshold=0.65,
        atr_multiplier=2.5,
        use_trend_filter=True,
        entry_mode="breakout",
        stop_loss_pct=0.08,
        trend_exit_days=5,
        profit_trail_activation_pct=0.18,
        profit_trail_drawdown_pct=0.15,
        min_hold_days=5,
    )

    _HIGH_BETA: ClassVar[TickerConfig] = TickerConfig(
        sma_period=10,
        rsi_buy_threshold=40.0,
        volume_threshold=0.55,
        atr_multiplier=3.0,
        use_trend_filter=True,
        entry_mode="breakout",
        stop_loss_pct=0.08,
        trend_exit_days=TREND_EXIT_DISABLED,
        profit_trail_activation_pct=0.20,
        profit_trail_drawdown_pct=0.15,
        min_hold_days=5,
        skip_trend_exit_when_ranked=True,
    )

    _MOMENTUM: ClassVar[TickerConfig] = TickerConfig(
        sma_period=10,
        rsi_buy_threshold=40.0,
        volume_threshold=0.60,
        atr_multiplier=3.5,
        use_trend_filter=False,
        entry_mode="breakout",
        stop_loss_pct=0.08,
        trend_exit_days=TREND_EXIT_DISABLED,
        profit_trail_activation_pct=0.15,
        profit_trail_drawdown_pct=0.18,
        min_hold_days=5,
        skip_trend_exit_when_ranked=True,
    )

    _DEFAULT: ClassVar[TickerConfig] = TickerConfig(
        sma_period=20,
        rsi_buy_threshold=48.0,
        volume_threshold=0.65,
        atr_multiplier=2.0,
        use_trend_filter=True,
        entry_mode="breakout",
        stop_loss_pct=0.08,
        trend_exit_days=5,
        min_hold_days=5,
    )

    _EXPLICIT: ClassVar[Mapping[str, TickerConfig]] = {
        "AAPL": _MEGA,
        "MSFT": _MEGA,
        "GOOGL": _MEGA,
        "AMZN": _MEGA,
        "NVDA": _HIGH_BETA,
        "META": _HIGH_BETA,
        "AVGO": _HIGH_BETA,
        "NFLX": _HIGH_BETA,
        "PLTR": _MOMENTUM,
        "TSLA": _MOMENTUM,
        "CRWD": _MOMENTUM,
        "AMD": _MOMENTUM,
    }

    MARKET_BENCHMARK_TICKER: ClassVar[str] = BENCHMARK_TICKER
    MARKET_BENCHMARK_SMA_PERIOD: ClassVar[int] = BENCHMARK_SMA_PERIOD

    @classmethod
    def use_spy_market_filter(cls) -> bool:
        return os.getenv("USE_SPY_MARKET_FILTER", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @classmethod
    def use_qqq_regime_filter(cls) -> bool:
        return os.getenv("USE_QQQ_REGIME_FILTER", "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @classmethod
    def resolve_regime(cls, ticker: str) -> TickerConfig:
        normalized = ticker.strip().upper()
        return cls._EXPLICIT.get(normalized, cls._DEFAULT)

    @classmethod
    def for_ticker(cls, ticker: str) -> StrategyConfig:
        normalized = ticker.strip().upper()
        regime = cls.resolve_regime(normalized)
        return StrategyConfig.from_ticker_config(normalized, regime)

    @classmethod
    def registered_tickers(cls) -> tuple[str, ...]:
        return tuple(sorted(cls._EXPLICIT.keys()))


StrategyConfigRegistry = StrategyConfigMapper
