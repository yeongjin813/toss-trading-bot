"""
Top-3 momentum shadow portfolio for Phase 3/4 dual-strategy deployment.

Phase 3: log/Telegram simulated orders only (no KIS).
Phase 4: may dispatch real orders using the Top3 capital slice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol

import pandas as pd

from deployment_config import DeploymentConfig, scaled_capital
from momentum_ranker import (
    MomentumRankSettings,
    rank_universe_cache,
    select_top_tickers,
    select_top_tickers_diversified,
    should_rebalance_on_bar_date,
)

TOP3_STATE_KEY = "_top3_shadow"


class FrameProvider(Protocol):
    def get_frame(self, ticker: str) -> Any: ...


@dataclass
class Top3ShadowState:
    holdings: dict[str, int] = field(default_factory=dict)
    cash_usd: float = 0.0
    last_rebalance_date: str | None = None
    active_tickers: list[str] = field(default_factory=list)
    last_equity_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "holdings": dict(self.holdings),
            "cash_usd": self.cash_usd,
            "last_rebalance_date": self.last_rebalance_date,
            "active_tickers": list(self.active_tickers),
            "last_equity_usd": self.last_equity_usd,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> Top3ShadowState:
        if not isinstance(payload, dict):
            return cls()
        holdings = payload.get("holdings") or {}
        return cls(
            holdings={str(k): int(v) for k, v in holdings.items()},
            cash_usd=float(payload.get("cash_usd", 0.0) or 0.0),
            last_rebalance_date=payload.get("last_rebalance_date"),
            active_tickers=[str(t) for t in (payload.get("active_tickers") or [])],
            last_equity_usd=float(payload.get("last_equity_usd", 0.0) or 0.0),
        )


def load_top3_state(states: dict[str, Any]) -> Top3ShadowState:
    portfolio = states.setdefault("_portfolio", {})
    raw = portfolio.get(TOP3_STATE_KEY)
    return Top3ShadowState.from_dict(raw if isinstance(raw, dict) else None)


def save_top3_state(states: dict[str, Any], shadow: Top3ShadowState) -> None:
    portfolio = states.setdefault("_portfolio", {})
    portfolio[TOP3_STATE_KEY] = shadow.to_dict()


def _price_from_frame(frame: Any, as_of_date: str) -> float | None:
    if frame is None or getattr(frame, "empty", True):
        return None
    work = frame.copy()
    if "Date" in work.columns:
        work["Date"] = pd.to_datetime(work["Date"])
        row = work[work["Date"] <= as_of_date]
        if row.empty:
            return None
        return float(row["Close"].iloc[-1])
    idx = work.index
    ts = pd.Timestamp(as_of_date)
    window = work.loc[idx <= ts]
    if window.empty:
        return None
    return float(window["Close"].iloc[-1])


@dataclass(frozen=True)
class Top3SimulatedOrder:
    ticker: str
    side: str
    shares: int
    reference_price: float
    reason: str


def compute_top3_rebalance_orders(
    cache: FrameProvider,
    universe: list[str],
    shadow: Top3ShadowState,
    *,
    total_equity_usd: float,
    deploy: DeploymentConfig,
    now: datetime | None = None,
    settings: MomentumRankSettings | None = None,
    force: bool = False,
    broker_holdings: Mapping[str, int] | None = None,
) -> tuple[Top3ShadowState, list[Top3SimulatedOrder], list[str]]:
    """
    Return updated shadow state and simulated orders for a rebalance pass.

    When broker_holdings is supplied (Phase 4 live), deltas use the shared
    broker account as source-of-truth instead of shadow-only quantities.
    """
    cfg = settings or MomentumRankSettings.from_env()
    cfg = MomentumRankSettings(
        enabled=True,
        top_n=cfg.top_n,
        rebalance_weekday=cfg.rebalance_weekday,
        weight_3m=cfg.weight_3m,
        weight_6m=cfg.weight_6m,
        weight_12m=cfg.weight_12m,
        weight_volume=cfg.weight_volume,
        require_above_sma50=cfg.require_above_sma50,
        require_above_sma200=cfg.require_above_sma200,
        min_bars=cfg.min_bars,
    )

    as_of = (now or datetime.now()).strftime("%Y-%m-%d")
    first_run = shadow.last_rebalance_date is None
    if not force and not first_run and not should_rebalance_on_bar_date(
        as_of,
        shadow.last_rebalance_date,
        rebalance_weekday=cfg.rebalance_weekday,
    ):
        return shadow, [], []

    ranked = rank_universe_cache(cache, universe, as_of_date=as_of, settings=cfg)
    if cfg.sector_diversify:
        target = select_top_tickers_diversified(
            ranked,
            top_n=cfg.top_n,
            max_per_sector=cfg.max_per_sector,
        )
    else:
        target = select_top_tickers(ranked, top_n=cfg.top_n)
    if not target:
        target = universe[: cfg.top_n]

    top3_equity = scaled_capital(total_equity_usd, deploy.top3_capital_fraction())
    if shadow.cash_usd <= 0 and not shadow.holdings:
        shadow.cash_usd = top3_equity

    prices: dict[str, float] = {}
    for ticker in universe:
        price = _price_from_frame(cache.get_frame(ticker), as_of)
        if price is not None:
            prices[ticker] = price

    holdings_value = sum(
        shadow.holdings.get(t, 0) * prices[t]
        for t in shadow.holdings
        if t in prices
    )
    equity = shadow.cash_usd + holdings_value
    if equity <= 0:
        equity = top3_equity
        shadow.cash_usd = top3_equity

    orders: list[Top3SimulatedOrder] = []
    logs: list[str] = []

    def current_shares(ticker: str) -> int:
        if broker_holdings is not None:
            return int(broker_holdings.get(ticker, 0) or 0)
        return int(shadow.holdings.get(ticker, 0) or 0)

    held_tickers: set[str] = set()
    if broker_holdings is not None:
        held_tickers = {
            ticker
            for ticker in universe
            if int(broker_holdings.get(ticker, 0) or 0) > 0
        }
    else:
        held_tickers = {t for t, q in shadow.holdings.items() if q > 0}

    for ticker in held_tickers:
        shares = current_shares(ticker)
        if shares > 0 and ticker not in target and ticker in prices:
            orders.append(
                Top3SimulatedOrder(
                    ticker=ticker,
                    side="SELL",
                    shares=shares,
                    reference_price=prices[ticker],
                    reason="dropped_from_topn",
                )
            )
            proceeds = shares * prices[ticker]
            shadow.cash_usd += proceeds
            shadow.holdings[ticker] = 0
            logs.append(f"[TOP3/SHADOW] SELL {shares} {ticker} @ {prices[ticker]:.2f}")

    holdings_value = sum(
        shadow.holdings.get(t, 0) * prices[t]
        for t in shadow.holdings
        if t in prices
    )
    equity = shadow.cash_usd + holdings_value
    per_slot = equity / len(target) if target else 0.0

    for ticker in target:
        if ticker not in prices:
            continue
        price = prices[ticker]
        current = current_shares(ticker)
        target_shares = int(per_slot / price) if price > 0 else 0
        delta = target_shares - current
        if delta < 0:
            sell_qty = -delta
            orders.append(
                Top3SimulatedOrder(
                    ticker=ticker,
                    side="SELL",
                    shares=sell_qty,
                    reference_price=price,
                    reason="rebalance_trim",
                )
            )
            shadow.cash_usd += sell_qty * price
            shadow.holdings[ticker] = current - sell_qty
            if shadow.holdings[ticker] <= 0:
                shadow.holdings.pop(ticker, None)
            logs.append(f"[TOP3/SHADOW] TRIM {sell_qty} {ticker} @ {price:.2f}")
        elif delta > 0:
            cost = delta * price
            if cost > shadow.cash_usd:
                delta = int(shadow.cash_usd / price) if price > 0 else 0
            if delta > 0:
                orders.append(
                    Top3SimulatedOrder(
                        ticker=ticker,
                        side="BUY",
                        shares=delta,
                        reference_price=price,
                        reason="rebalance_buy",
                    )
                )
                shadow.cash_usd -= delta * price
                shadow.holdings[ticker] = current + delta
                logs.append(f"[TOP3/SHADOW] BUY {delta} {ticker} @ {price:.2f}")

    shadow.last_rebalance_date = as_of
    shadow.active_tickers = list(target)
    shadow.last_equity_usd = shadow.cash_usd + sum(
        shadow.holdings.get(t, 0) * prices[t] for t in shadow.holdings if t in prices
    )
    logs.insert(
        0,
        f"[TOP3/SHADOW] Rebalance {as_of} Top{cfg.top_n}: {', '.join(target)} "
        f"(equity=${shadow.last_equity_usd:,.2f})",
    )
    return shadow, orders, logs


def persist_top3_state_file(shadow: Top3ShadowState, path: str | Path) -> None:
    target = Path(path)
    target.write_text(json.dumps(shadow.to_dict(), indent=2), encoding="utf-8")
