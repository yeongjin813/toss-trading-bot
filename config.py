"""
Ticker-specific strategy configuration isolation.

Execution parameters resolve exclusively through StrategyConfigMapper.for_ticker().
Global .env SMA/RSI/ATR overrides are not consulted for signal generation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import ClassVar, Mapping

from market_registry import BENCHMARK_SMA_PERIOD, BENCHMARK_TICKER


@dataclass(frozen=True)
class TickerConfig:
    """
    Regime-scoped parameter bundle keyed to volatility/trend personality.

    Fields map directly to the optimization matrix:
      sma_period       — short-term crossover window (entry/exit SMA)
      rsi_buy_threshold — minimum RSI for long entry authorization
      volume_threshold  — volume_mult: current volume / volume_sma gate
      atr_multiplier    — ATR trailing stop width multiplier
      use_trend_filter  — enable 50-day SMA regime gate + conditional RSI exit
    """

    sma_period: int
    rsi_buy_threshold: float
    volume_threshold: float
    atr_multiplier: float
    use_trend_filter: bool


@dataclass(frozen=True)
class StrategyConfig:
    """
    Fully resolved, ticker-bound configuration for live and backtest engines.

    sma_period is ticker-isolated (short SMA / Golden-Death cross).
    sma_long_period is fixed at 50 (macro regime filter baseline).
    """

    ticker: str
    sma_period: int
    rsi_buy_threshold: float
    volume_threshold: float
    atr_multiplier: float
    use_trend_filter: bool
    rsi_period: int = 14
    atr_period: int = 14
    volume_sma_period: int = 20
    sma_long_period: int = 50
    rsi_exit_threshold: float = 50.0
    use_trailing_stop: bool = True

    @classmethod
    def from_ticker_config(cls, ticker: str, regime: TickerConfig) -> StrategyConfig:
        """Materialize a StrategyConfig from an isolated TickerConfig regime."""
        return cls(
            ticker=ticker.upper(),
            sma_period=regime.sma_period,
            rsi_buy_threshold=regime.rsi_buy_threshold,
            volume_threshold=regime.volume_threshold,
            atr_multiplier=regime.atr_multiplier,
            use_trend_filter=regime.use_trend_filter,
        )


class StrategyConfigMapper:
    """
    Parameter isolation matrix: ticker symbol -> StrategyConfig.

    Hardcoded optimized regimes (no .env execution overrides):

      NVDA    — high volatility / strong trend
      PLTR    — high volatility / breakout (no trend filter)
      DEFAULT — conservative baseline (AAPL and all unlisted symbols)
    """

    SMA_LONG_PERIOD: ClassVar[int] = 50

    _NVDA: ClassVar[TickerConfig] = TickerConfig(
        sma_period=10,
        rsi_buy_threshold=40.0,
        volume_threshold=0.55,
        atr_multiplier=3.0,
        use_trend_filter=True,
    )

    _PLTR: ClassVar[TickerConfig] = TickerConfig(
        sma_period=10,
        rsi_buy_threshold=42.0,
        volume_threshold=0.60,
        atr_multiplier=2.5,
        use_trend_filter=False,
    )

    _DEFAULT: ClassVar[TickerConfig] = TickerConfig(
        sma_period=20,
        rsi_buy_threshold=48.0,
        volume_threshold=0.65,
        atr_multiplier=2.0,
        use_trend_filter=True,
    )

    _EXPLICIT: ClassVar[Mapping[str, TickerConfig]] = {
        "NVDA": _NVDA,
        "PLTR": _PLTR,
        "TSLA": _PLTR,
        "AMD": _PLTR,
        "CRWD": _PLTR,
        "META": _NVDA,
        "NFLX": _NVDA,
        "AVGO": _NVDA,
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
    def resolve_regime(cls, ticker: str) -> TickerConfig:
        """Return the hardcoded TickerConfig for a symbol."""
        normalized = ticker.strip().upper()
        return cls._EXPLICIT.get(normalized, cls._DEFAULT)

    @classmethod
    def for_ticker(cls, ticker: str) -> StrategyConfig:
        """Return the fully resolved StrategyConfig for execution."""
        normalized = ticker.strip().upper()
        regime = cls.resolve_regime(normalized)
        return StrategyConfig.from_ticker_config(normalized, regime)

    @classmethod
    def registered_tickers(cls) -> tuple[str, ...]:
        """Symbols with explicit regime mappings (others use DEFAULT)."""
        return tuple(sorted(cls._EXPLICIT.keys()))


# Backward-compatible alias
StrategyConfigRegistry = StrategyConfigMapper
