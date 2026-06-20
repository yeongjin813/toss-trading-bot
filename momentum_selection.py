"""
Enhanced momentum selection factors (multi-period momentum, FIP, skewness, inverse-vol).

**RESEARCH / BACKTEST ONLY — not deployed to production.**

Production uses ``legacy`` ranking in ``momentum_ranker.py`` (63/126/252 + volume).
This module is retained for:
  - ``python run_backtest.py --compare-momentum-ranking``
  - ``python scripts/compare_momentum_ranking.py``
  - future shadow experiments

2020–2026 backtest (25-ticker watchlist): Legacy outperformed Enhanced on CAGR,
Sharpe, and drawdown (notably in the 2022 downturn). See README Phase 16.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

LOOKBACK_MOM_SHORT = 60
LOOKBACK_MOM_MID = 120
LOOKBACK_MOM_LONG = 252
LOOKBACK_FIP = 252
LOOKBACK_SKEW = 90
LOOKBACK_VOL = 126


@dataclass(frozen=True)
class TickerFactorRow:
    ticker: str
    ret_60: float
    ret_120: float
    ret_252: float
    momentum_score: float
    fip_score: float
    skewness_90d: float
    skewness_penalty: float
    vol_126d: float
    composite_score: float
    close: float
    above_sma50: bool
    above_sma200: bool


def daily_returns(closes: pd.Series) -> pd.Series:
    return closes.astype(float).pct_change().dropna()


def period_return(closes: pd.Series, lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    start = float(closes.iloc[-lookback - 1])
    end = float(closes.iloc[-1])
    if start <= 0:
        return None
    return (end / start) - 1.0


def frog_in_the_pan_score(returns: pd.Series, window: int = LOOKBACK_FIP) -> float | None:
    """Share of positive daily returns over the lookback window."""
    if len(returns) < window:
        return None
    windowed = returns.tail(window)
    if windowed.empty:
        return None
    return float((windowed > 0).sum()) / float(len(windowed))


def rolling_return_skewness(returns: pd.Series, window: int = LOOKBACK_SKEW) -> float | None:
    if len(returns) < window:
        return None
    skew = returns.tail(window).skew()
    if pd.isna(skew):
        return 0.0
    return float(skew)


def historical_volatility(returns: pd.Series, window: int = LOOKBACK_VOL) -> float | None:
    if len(returns) < window:
        return None
    windowed = returns.tail(window)
    vol = windowed.std(ddof=1)
    if pd.isna(vol) or vol <= 0:
        vol = float(windowed.abs().mean())
    if vol <= 0:
        return 1e-6
    return float(vol)


def skewness_penalty(skew: float) -> float:
    """Penalize left-tail risk; smooth, capped transform for live stability."""
    if skew >= 0:
        return 0.0
    return min((-skew) ** 1.25, 3.0)


def _normalize_map(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    vmin = min(values.values())
    vmax = max(values.values())
    if vmax == vmin:
        return {key: 0.5 for key in values}
    span = vmax - vmin
    return {key: (value - vmin) / span for key, value in values.items()}


def inverse_volatility_weights(
    vols: Mapping[str, float],
    *,
    min_vol: float = 1e-6,
) -> dict[str, float]:
    """weight_i = (1/vol_i) / sum(1/vol_j)."""
    if not vols:
        return {}
    inv = {ticker: 1.0 / max(vol, min_vol) for ticker, vol in vols.items()}
    total = sum(inv.values())
    if total <= 0:
        equal = 1.0 / len(vols)
        return {ticker: equal for ticker in vols}
    return {ticker: value / total for ticker, value in inv.items()}


def target_set_unchanged(previous: list[str], new: list[str]) -> bool:
    return set(previous) == set(new)


def compute_ticker_factors(
    closes: pd.Series,
    ticker: str,
    *,
    above_sma50: bool,
    above_sma200: bool,
) -> dict[str, float] | None:
    """Raw factor inputs for one ticker (before cross-sectional normalization)."""
    returns = daily_returns(closes)
    ret_60 = period_return(closes, LOOKBACK_MOM_SHORT)
    ret_120 = period_return(closes, LOOKBACK_MOM_MID)
    ret_252 = period_return(closes, LOOKBACK_MOM_LONG)
    fip = frog_in_the_pan_score(returns, LOOKBACK_FIP)
    skew = rolling_return_skewness(returns, LOOKBACK_SKEW)
    vol = historical_volatility(returns, LOOKBACK_VOL)
    if None in (ret_60, ret_120, ret_252, fip, skew, vol):
        return None
    return {
        "ticker": ticker,
        "ret_60": ret_60,
        "ret_120": ret_120,
        "ret_252": ret_252,
        "fip_score": fip,
        "skewness_90d": skew,
        "skewness_penalty": skewness_penalty(skew),
        "vol_126d": vol,
        "close": float(closes.iloc[-1]),
        "above_sma50": above_sma50,
        "above_sma200": above_sma200,
    }


def build_composite_ranking(
    raw_rows: Mapping[str, dict[str, float]],
    *,
    weight_momentum: float = 1.0,
    weight_fip: float = 1.0,
    weight_skew_penalty: float = 1.0,
) -> list[TickerFactorRow]:
    """
    Composite = normalized momentum + normalized FIP - normalized skew penalty.

    Momentum score = average of cross-sectionally normalized 60/120/252-day returns.
    """
    if not raw_rows:
        return []

    norm_60 = _normalize_map({t: row["ret_60"] for t, row in raw_rows.items()})
    norm_120 = _normalize_map({t: row["ret_120"] for t, row in raw_rows.items()})
    norm_252 = _normalize_map({t: row["ret_252"] for t, row in raw_rows.items()})
    norm_fip = _normalize_map({t: row["fip_score"] for t, row in raw_rows.items()})
    norm_skew_pen = _normalize_map(
        {t: row["skewness_penalty"] for t, row in raw_rows.items()}
    )

    ranked: list[TickerFactorRow] = []
    for ticker, row in raw_rows.items():
        momentum_score = (norm_60[ticker] + norm_120[ticker] + norm_252[ticker]) / 3.0
        composite = (
            weight_momentum * momentum_score
            + weight_fip * norm_fip[ticker]
            - weight_skew_penalty * norm_skew_pen[ticker]
        )
        ranked.append(
            TickerFactorRow(
                ticker=ticker,
                ret_60=row["ret_60"],
                ret_120=row["ret_120"],
                ret_252=row["ret_252"],
                momentum_score=momentum_score,
                fip_score=row["fip_score"],
                skewness_90d=row["skewness_90d"],
                skewness_penalty=row["skewness_penalty"],
                vol_126d=row["vol_126d"],
                composite_score=composite,
                close=row["close"],
                above_sma50=bool(row["above_sma50"]),
                above_sma200=bool(row["above_sma200"]),
            )
        )

    ranked.sort(key=lambda item: item.composite_score, reverse=True)
    return ranked


def target_allocation_weights(
    target: list[str],
    vol_by_ticker: Mapping[str, float],
    *,
    use_inverse_vol: bool,
) -> dict[str, float]:
    """Equal-weight (legacy) or inverse-vol weights for Top-N allocation."""
    if not target:
        return {}
    if use_inverse_vol:
        vols = {ticker: vol_by_ticker[ticker] for ticker in target if vol_by_ticker.get(ticker, 0) > 0}
        if len(vols) == len(target):
            return inverse_volatility_weights(vols)
    equal = 1.0 / len(target)
    return {ticker: equal for ticker in target}


def vol_map_from_ranked(
    ranked: list[Any],
    target: list[str],
) -> dict[str, float]:
    """Extract 126d vol from ranked rows (``MomentumScore.vol_126d``)."""
    by_ticker = {row.ticker: float(getattr(row, "vol_126d", 0.0) or 0.0) for row in ranked}
    return {ticker: by_ticker.get(ticker, 0.0) for ticker in target}


def print_momentum_ranking_comparison_table(
    legacy: Mapping[str, float | int | dict[str, float]],
    enhanced: Mapping[str, float | int | dict[str, float]],
    *,
    window_label: str = "",
) -> None:
    """Print legacy vs enhanced Top3 selection model metrics."""
    width = 78
    title = "MOMENTUM RANKING: LEGACY vs ENHANCED"
    if window_label:
        title = f"{title} ({window_label})"
    print("=" * width)
    print(title.center(width))
    print("=" * width)
    print(f"{'Metric':<24} {'Legacy':>14} {'Enhanced':>14} {'Delta':>14}")
    print("-" * width)

    def _fmt(label: str, leg: float, enh: float, *, money: bool = False, integer: bool = False) -> None:
        if money:
            print(f"{label:<24} ${leg:>12,.2f} ${enh:>12,.2f} ${enh - leg:>+12,.2f}")
        elif integer:
            print(f"{label:<24} {int(leg):>14} {int(enh):>14} {int(enh - leg):>+14}")
        else:
            print(f"{label:<24} {leg:>+13.2f} {enh:>+13.2f} {enh - leg:>+13.2f}")

    rows = [
        ("CAGR %", float(legacy["cagr_pct"]), float(enhanced["cagr_pct"])),
        ("Total Return %", float(legacy["total_return_pct"]), float(enhanced["total_return_pct"])),
        ("Sharpe", float(legacy["sharpe"]), float(enhanced["sharpe"])),
        ("Max Drawdown %", float(legacy["max_drawdown_pct"]), float(enhanced["max_drawdown_pct"])),
        ("Win Rate %", float(legacy["win_rate_pct"]), float(enhanced["win_rate_pct"])),
        ("Profit Factor", float(legacy["profit_factor"]), float(enhanced["profit_factor"])),
        ("Total Trades", float(legacy["total_trades"]), float(enhanced["total_trades"]), True),
        ("Rebalance Events", float(legacy.get("rebalance_count", 0)), float(enhanced.get("rebalance_count", 0)), True),
    ]
    for row in rows:
        if len(row) == 4 and row[3] is True:
            _fmt(row[0], row[1], row[2], integer=True)
        else:
            _fmt(row[0], row[1], row[2])

    leg_annual = legacy.get("annual_returns_pct") or {}
    enh_annual = enhanced.get("annual_returns_pct") or {}
    years = sorted(set(leg_annual) | set(enh_annual))
    if years:
        print("-" * width)
        print("Annual Returns %")
        for year in years:
            leg = float(leg_annual.get(year, 0.0))
            enh = float(enh_annual.get(year, 0.0))
            _fmt(str(year), leg, enh)
    print("=" * width)


def summarize_top3_analytics(
    *,
    initial_cash: float,
    final_equity: float,
    equity_curve: pd.DataFrame,
    closed_pnls: list[float],
    total_trades: int,
    winning_trades: int,
) -> dict[str, float | int | dict[str, float]]:
    """CAGR, Sharpe, MaxDD, win rate, profit factor, annual returns."""
    from portfolio_backtest import compute_max_drawdown, compute_sharpe_ratio

    max_dd = 0.0
    sharpe = 0.0
    cagr_pct = 0.0
    annual_returns: dict[str, float] = {}

    if not equity_curve.empty and "equity" in equity_curve.columns:
        series = equity_curve["equity"].astype(float)
        max_dd = compute_max_drawdown(series)
        sharpe = compute_sharpe_ratio(series)
        if len(series) >= 2:
            start = series.index[0]
            end = series.index[-1]
            years = max((end - start).days / 365.25, 1 / 365.25)
            if initial_cash > 0 and final_equity > 0:
                cagr_pct = (math.pow(final_equity / initial_cash, 1.0 / years) - 1.0) * 100.0
            by_year = series.groupby(series.index.year)
            for year, chunk in by_year:
                if len(chunk) < 2:
                    continue
                annual_returns[str(year)] = (chunk.iloc[-1] / chunk.iloc[0] - 1.0) * 100.0

    gross_profit = sum(p for p in closed_pnls if p > 0)
    gross_loss = abs(sum(p for p in closed_pnls if p < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    total_return_pct = (
        (final_equity / initial_cash - 1.0) * 100.0 if initial_cash > 0 else 0.0
    )
    win_rate = (winning_trades / total_trades * 100.0) if total_trades else 0.0

    return {
        "cagr_pct": cagr_pct,
        "total_return_pct": total_return_pct,
        "sharpe": sharpe,
        "max_drawdown_pct": max_dd,
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "total_trades": total_trades,
        "annual_returns_pct": annual_returns,
    }
