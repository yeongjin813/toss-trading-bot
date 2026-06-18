"""
Cross-sectional momentum ranker for universe -> active tradable subset.

Scores each symbol on multi-horizon returns, trend alignment, and volume
stability, then selects Top N names for new BUY eligibility only. Held
positions outside the active set still receive exit processing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol

import pandas as pd

from analytics import _to_ny_datetime

TRADING_DAYS_3M = 63
TRADING_DAYS_6M = 126
TRADING_DAYS_12M = 252
SMA_50 = 50
SMA_200 = 200


class FrameProvider(Protocol):
    def get_frame(self, ticker: str) -> pd.DataFrame: ...


@dataclass(frozen=True)
class MomentumRankSettings:
    enabled: bool = True
    top_n: int = 3
    rebalance_weekday: int = 4  # Friday (Mon=0)
    weight_3m: float = 0.4
    weight_6m: float = 0.3
    weight_12m: float = 0.2
    weight_volume: float = 0.1
    require_above_sma50: bool = True
    require_above_sma200: bool = False
    min_bars: int = TRADING_DAYS_12M

    @classmethod
    def from_env(cls) -> MomentumRankSettings:
        def _flag(name: str, default: str = "true") -> bool:
            return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            enabled=_flag("MOMENTUM_RANK_ENABLED", "true"),
            top_n=max(1, int(os.getenv("MOMENTUM_TOP_N", "3"))),
            rebalance_weekday=int(os.getenv("MOMENTUM_REBALANCE_WEEKDAY", "4")),
            weight_3m=float(os.getenv("MOMENTUM_WEIGHT_3M", "0.4")),
            weight_6m=float(os.getenv("MOMENTUM_WEIGHT_6M", "0.3")),
            weight_12m=float(os.getenv("MOMENTUM_WEIGHT_12M", "0.2")),
            weight_volume=float(os.getenv("MOMENTUM_WEIGHT_VOLUME", "0.1")),
            require_above_sma50=_flag("MOMENTUM_REQUIRE_SMA50", "true"),
            require_above_sma200=_flag("MOMENTUM_REQUIRE_SMA200", "false"),
            min_bars=int(os.getenv("MOMENTUM_MIN_BARS", str(TRADING_DAYS_12M))),
        )


@dataclass
class MomentumScore:
    ticker: str
    score: float
    ret_3m: float
    ret_6m: float
    ret_12m: float
    above_sma50: bool
    above_sma200: bool
    volume_score: float
    close: float


@dataclass
class MomentumRankSnapshot:
    as_of_date: str
    active_tickers: list[str]
    scores: list[MomentumScore] = field(default_factory=list)


def _date_column(df: pd.DataFrame) -> pd.Series:
    if "Date" in df.columns:
        return pd.to_datetime(df["Date"])
    return pd.to_datetime(df.index)


def _slice_as_of(df: pd.DataFrame, as_of_date: str | None) -> pd.DataFrame:
    if df.empty:
        return df
    work = df.copy()
    work["_asof"] = _date_column(work)
    if as_of_date:
        cutoff = pd.Timestamp(as_of_date)
        work = work[work["_asof"] <= cutoff]
    return work.drop(columns=["_asof"], errors="ignore")


def _period_return(closes: pd.Series, lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    start = float(closes.iloc[-lookback - 1])
    end = float(closes.iloc[-1])
    if start <= 0:
        return None
    return (end / start) - 1.0


def _volume_stability_score(volumes: pd.Series) -> float:
    if len(volumes) < 40:
        return 0.0
    recent = volumes.tail(20).astype(float)
    baseline = volumes.tail(60).astype(float)
    if recent.mean() <= 0 or baseline.mean() <= 0:
        return 0.0
    ratio = min(float(recent.mean() / baseline.mean()), 2.0) / 2.0
    volatility = float(recent.std(ddof=0) / recent.mean()) if recent.mean() else 1.0
    stability = 1.0 / (1.0 + volatility)
    return max(0.0, min(1.0, 0.6 * ratio + 0.4 * stability))


def _normalize_map(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    vmin = min(values.values())
    vmax = max(values.values())
    if vmax == vmin:
        return {key: 0.5 for key in values}
    span = vmax - vmin
    return {key: (value - vmin) / span for key, value in values.items()}


def compute_ticker_momentum(
    df: pd.DataFrame,
    ticker: str,
    *,
    as_of_date: str | None = None,
    settings: MomentumRankSettings | None = None,
) -> MomentumScore | None:
    """Return raw momentum components for one ticker, or None if filtered out."""
    cfg = settings or MomentumRankSettings.from_env()
    window = _slice_as_of(df, as_of_date)
    if len(window) < cfg.min_bars:
        return None

    closes = window["Close"].astype(float)
    volumes = window["Volume"].astype(float)
    close = float(closes.iloc[-1])

    sma50 = float(closes.tail(SMA_50).mean()) if len(closes) >= SMA_50 else close
    sma200 = float(closes.tail(SMA_200).mean()) if len(closes) >= SMA_200 else close
    above_sma50 = close > sma50
    above_sma200 = close > sma200

    if cfg.require_above_sma50 and not above_sma50:
        return None
    if cfg.require_above_sma200 and not above_sma200:
        return None

    ret_3m = _period_return(closes, TRADING_DAYS_3M)
    ret_6m = _period_return(closes, TRADING_DAYS_6M)
    ret_12m = _period_return(closes, TRADING_DAYS_12M)
    if ret_3m is None or ret_6m is None or ret_12m is None:
        return None

    return MomentumScore(
        ticker=ticker,
        score=0.0,
        ret_3m=ret_3m,
        ret_6m=ret_6m,
        ret_12m=ret_12m,
        above_sma50=above_sma50,
        above_sma200=above_sma200,
        volume_score=_volume_stability_score(volumes),
        close=close,
    )


def rank_universe_frames(
    frames: Mapping[str, pd.DataFrame],
    universe: list[str],
    *,
    as_of_date: str | None = None,
    settings: MomentumRankSettings | None = None,
) -> list[MomentumScore]:
    """Score and sort the universe; highest composite score first."""
    cfg = settings or MomentumRankSettings.from_env()
    raw: dict[str, MomentumScore] = {}

    for ticker in universe:
        frame = frames.get(ticker)
        if frame is None or frame.empty:
            continue
        scored = compute_ticker_momentum(
            frame,
            ticker,
            as_of_date=as_of_date,
            settings=cfg,
        )
        if scored is not None:
            raw[ticker] = scored

    if not raw:
        return []

    norm_3m = _normalize_map({t: s.ret_3m for t, s in raw.items()})
    norm_6m = _normalize_map({t: s.ret_6m for t, s in raw.items()})
    norm_12m = _normalize_map({t: s.ret_12m for t, s in raw.items()})

    ranked: list[MomentumScore] = []
    for ticker, item in raw.items():
        composite = (
            cfg.weight_3m * norm_3m[ticker]
            + cfg.weight_6m * norm_6m[ticker]
            + cfg.weight_12m * norm_12m[ticker]
            + cfg.weight_volume * item.volume_score
        )
        ranked.append(
            MomentumScore(
                ticker=ticker,
                score=composite,
                ret_3m=item.ret_3m,
                ret_6m=item.ret_6m,
                ret_12m=item.ret_12m,
                above_sma50=item.above_sma50,
                above_sma200=item.above_sma200,
                volume_score=item.volume_score,
                close=item.close,
            )
        )

    ranked.sort(key=lambda row: row.score, reverse=True)
    return ranked


def rank_universe_cache(
    cache: FrameProvider,
    universe: list[str],
    *,
    as_of_date: str | None = None,
    settings: MomentumRankSettings | None = None,
) -> list[MomentumScore]:
    frames = {ticker: cache.get_frame(ticker) for ticker in universe}
    return rank_universe_frames(
        frames,
        universe,
        as_of_date=as_of_date,
        settings=settings,
    )


def select_top_tickers(
    ranked: list[MomentumScore],
    *,
    top_n: int,
) -> list[str]:
    return [row.ticker for row in ranked[:top_n]]


def should_rebalance_today(
    now: datetime | None,
    last_rebalance_date: str | None,
    *,
    rebalance_weekday: int,
) -> bool:
    ny = _to_ny_datetime(now)
    today = ny.strftime("%Y-%m-%d")
    if ny.weekday() != rebalance_weekday:
        return False
    if last_rebalance_date == today:
        return False
    return True


def held_or_pending_tickers(
    universe: list[str],
    states: Mapping[str, Any],
) -> set[str]:
    active: set[str] = set()
    for ticker in universe:
        payload = states.get(ticker, {})
        if not isinstance(payload, dict):
            continue
        if int(payload.get("held_quantity", 0) or 0) > 0:
            active.add(ticker)
            continue
        if payload.get("pending_order") or payload.get("open_order_id"):
            active.add(ticker)
    return active


def build_cycle_tickers(
    universe: list[str],
    active_trade_tickers: list[str],
    states: Mapping[str, Any],
) -> list[str]:
    """Universe order preserved; flat names outside active set are skipped."""
    must_run = held_or_pending_tickers(universe, states)
    allowed = set(active_trade_tickers) | must_run
    return [ticker for ticker in universe if ticker in allowed]


def is_new_buy_allowed(
    ticker: str,
    active_trade_tickers: set[str] | frozenset[str],
    *,
    settings: MomentumRankSettings | None = None,
) -> bool:
    cfg = settings or MomentumRankSettings.from_env()
    if not cfg.enabled:
        return True
    return ticker in active_trade_tickers


def rebalance_active_tickers(
    cache: FrameProvider,
    universe: list[str],
    states: dict[str, Any],
    *,
    now: datetime | None = None,
    settings: MomentumRankSettings | None = None,
    force: bool = False,
) -> MomentumRankSnapshot:
    """Recompute Top N on rebalance day and persist under states['_portfolio']."""
    cfg = settings or MomentumRankSettings.from_env()
    ny = _to_ny_datetime(now)
    as_of_date = ny.strftime("%Y-%m-%d")
    portfolio = states.setdefault("_portfolio", {})
    last_rebalance = portfolio.get("momentum_last_rebalance_date")

    if not cfg.enabled:
        active = list(universe)
        snapshot = MomentumRankSnapshot(as_of_date=as_of_date, active_tickers=active)
        portfolio["active_trade_tickers"] = active
        return snapshot

    if not force and not should_rebalance_today(
        ny,
        last_rebalance,
        rebalance_weekday=cfg.rebalance_weekday,
    ):
        cached = portfolio.get("active_trade_tickers")
        if cached:
            return MomentumRankSnapshot(
                as_of_date=as_of_date,
                active_tickers=list(cached),
            )

    ranked = rank_universe_cache(
        cache,
        universe,
        as_of_date=as_of_date,
        settings=cfg,
    )
    active = select_top_tickers(ranked, top_n=cfg.top_n)
    if not active:
        active = list(universe[: cfg.top_n])

    portfolio["active_trade_tickers"] = active
    portfolio["momentum_last_rebalance_date"] = as_of_date
    portfolio["momentum_rankings"] = [
        {
            "ticker": row.ticker,
            "score": round(row.score, 4),
            "ret_3m_pct": round(row.ret_3m * 100.0, 2),
            "ret_6m_pct": round(row.ret_6m * 100.0, 2),
            "ret_12m_pct": round(row.ret_12m * 100.0, 2),
            "above_sma50": row.above_sma50,
            "above_sma200": row.above_sma200,
            "volume_score": round(row.volume_score, 4),
        }
        for row in ranked[: max(cfg.top_n * 2, cfg.top_n)]
    ]

    print(
        f"[MOMENTUM] Rebalance {as_of_date} Top {cfg.top_n}: "
        f"{', '.join(active)}"
    )
    for row in ranked[: cfg.top_n]:
        print(
            f"  {row.ticker:<5} score={row.score:.3f} "
            f"3M={row.ret_3m * 100:+.1f}% 6M={row.ret_6m * 100:+.1f}% "
            f"12M={row.ret_12m * 100:+.1f}% vol={row.volume_score:.2f}"
        )

    return MomentumRankSnapshot(
        as_of_date=as_of_date,
        active_tickers=active,
        scores=ranked,
    )


def active_trade_set_from_states(states: Mapping[str, Any]) -> set[str]:
    portfolio = states.get("_portfolio", {})
    if not isinstance(portfolio, dict):
        return set()
    raw = portfolio.get("active_trade_tickers") or []
    return {str(ticker).upper() for ticker in raw}
