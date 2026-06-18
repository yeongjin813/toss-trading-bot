"""
Standalone Top-3 momentum portfolio backtest.

Equal-weight hold of momentum Top-N; rebalance on Fridays; exit when dropped
from Top-N on rebalance day. Separate from the legacy LiveSignalEngine path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from momentum_ranker import (
    MomentumRankSettings,
    rank_universe_frames,
    select_top_tickers,
    should_rebalance_on_bar_date,
)
from portfolio_backtest import (
    TradeRecord,
    compute_max_drawdown,
    compute_portfolio_equity,
    compute_sharpe_ratio,
)


@dataclass
class Top3BacktestResult:
    initial_cash: float
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    sharpe_ratio: float
    total_trades: int
    winning_trades: int
    equity_curve: pd.DataFrame
    trades: list[TradeRecord]
    per_ticker_summary: dict[str, dict[str, Any]]
    rebalance_count: int = 0


def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    if "Date" in frame.columns:
        frame["Date"] = pd.to_datetime(frame["Date"])
        frame = frame.sort_values("Date").set_index("Date")
    elif not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
        frame = frame.sort_index()
    return frame


def _close_on_date(df: pd.DataFrame, bar_date: str) -> float | None:
    frame = _normalize_frame(df)
    ts = pd.Timestamp(bar_date)
    if ts not in frame.index:
        window = frame.loc[frame.index <= ts]
        if window.empty:
            return None
        return float(window["Close"].iloc[-1])
    return float(frame.loc[ts, "Close"])


def run_top3_backtest(
    tickers: list[str],
    ohlcv_by_ticker: dict[str, pd.DataFrame],
    initial_cash: float = 10_000.0,
    commission_rate: float = 0.001,
    momentum_settings: MomentumRankSettings | None = None,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
) -> Top3BacktestResult:
    """
    Simulate equal-weight Top-N momentum portfolio with Friday rebalance.

    On each rebalance day:
      1. Rank universe, select Top N
      2. Sell holdings outside Top N
      3. Rebalance to equal weight across Top N (target = equity / N each)
    """
    cfg = momentum_settings or MomentumRankSettings.from_env()
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

    raw_frames = {t: _normalize_frame(ohlcv_by_ticker[t]) for t in tickers}
    all_dates: set[str] = set()
    for frame in raw_frames.values():
        all_dates.update(d.strftime("%Y-%m-%d") for d in frame.index)
    timeline = sorted(all_dates)
    if window_start or window_end:
        start_ts = pd.Timestamp(window_start or timeline[0])
        end_ts = pd.Timestamp(window_end or timeline[-1])
        timeline = [
            d for d in timeline if start_ts <= pd.Timestamp(d) <= end_ts
        ]

    cash = initial_cash
    holdings: dict[str, int] = {}
    trades: list[TradeRecord] = []
    equity_rows: list[dict[str, Any]] = []
    closed_pnls: list[float] = []
    open_cost: dict[str, float] = {}
    last_rebalance: str | None = None
    rebalance_count = 0

    def mark_prices(bar_date: str) -> dict[str, float]:
        prices: dict[str, float] = {}
        for ticker in tickers:
            price = _close_on_date(raw_frames[ticker], bar_date)
            if price is not None:
                prices[ticker] = price
        return prices

    def holdings_value(prices: dict[str, float]) -> dict[str, tuple[int, float]]:
        return {
            t: (shares, prices[t])
            for t, shares in holdings.items()
            if shares > 0 and t in prices
        }

    def do_sell(ticker: str, shares: int, price: float, bar_date: str, signal: str) -> None:
        nonlocal cash
        held = holdings.get(ticker, 0)
        if shares <= 0 or held <= 0:
            return
        shares = min(shares, held)
        gross = shares * price
        commission = gross * commission_rate
        proceeds = gross - commission
        total_cost = open_cost.get(ticker, held * price)
        cost_basis = total_cost * (shares / held)
        closed_pnls.append(proceeds - cost_basis)
        cash += proceeds
        remaining = held - shares
        if remaining > 0:
            holdings[ticker] = remaining
            open_cost[ticker] = total_cost - cost_basis
        else:
            holdings.pop(ticker, None)
            open_cost.pop(ticker, None)
        hv = holdings_value({ticker: price})
        equity = compute_portfolio_equity(cash, hv)
        trades.append(
            TradeRecord(
                date=bar_date,
                ticker=ticker,
                side="SELL",
                shares=shares,
                price=price,
                commission=commission,
                cash_after=cash,
                equity_after=equity,
                signal=signal,
                exit_reason="dropped_from_topn",
            )
        )

    def do_buy(ticker: str, shares: int, price: float, bar_date: str) -> bool:
        nonlocal cash
        if shares <= 0:
            return False
        gross = shares * price
        commission = gross * commission_rate
        total_cost = gross + commission
        if total_cost > cash:
            shares = int(cash / (price * (1.0 + commission_rate)))
            if shares <= 0:
                return False
            gross = shares * price
            commission = gross * commission_rate
            total_cost = gross + commission
        cash -= total_cost
        holdings[ticker] = holdings.get(ticker, 0) + shares
        open_cost[ticker] = open_cost.get(ticker, 0.0) + total_cost
        hv = holdings_value({ticker: price})
        equity = compute_portfolio_equity(cash, hv)
        trades.append(
            TradeRecord(
                date=bar_date,
                ticker=ticker,
                side="BUY",
                shares=shares,
                price=price,
                commission=commission,
                cash_after=cash,
                equity_after=equity,
                signal="TOP3_REBALANCE",
            )
        )
        return True

    for bar_date in timeline:
        first_run = last_rebalance is None
        is_rebalance = first_run or should_rebalance_on_bar_date(
            bar_date,
            last_rebalance,
            rebalance_weekday=cfg.rebalance_weekday,
        )

        if is_rebalance:
            ranked = rank_universe_frames(
                raw_frames,
                tickers,
                as_of_date=bar_date,
                settings=cfg,
            )
            target = select_top_tickers(ranked, top_n=cfg.top_n)
            if not target:
                target = tickers[: cfg.top_n]

            prices = mark_prices(bar_date)
            hv = holdings_value(prices)
            equity = compute_portfolio_equity(cash, hv)

            for ticker, shares in list(holdings.items()):
                if shares > 0 and ticker not in target and ticker in prices:
                    do_sell(ticker, shares, prices[ticker], bar_date, "TOP3_EXIT")

            prices = mark_prices(bar_date)
            hv = holdings_value(prices)
            equity = compute_portfolio_equity(cash, hv)
            per_slot = equity / len(target) if target else 0.0

            for ticker in target:
                if ticker not in prices or prices[ticker] <= 0:
                    continue
                price = prices[ticker]
                current_shares = holdings.get(ticker, 0)
                target_shares = int(per_slot / price)
                delta = target_shares - current_shares
                if delta < 0:
                    do_sell(ticker, -delta, price, bar_date, "TOP3_TRIM")
                elif delta > 0:
                    do_buy(ticker, delta, price, bar_date)

            last_rebalance = bar_date
            rebalance_count += 1

        prices = mark_prices(bar_date)
        hv = holdings_value(prices)
        equity = compute_portfolio_equity(cash, hv)
        equity_rows.append(
            {
                "date": bar_date,
                "cash": cash,
                "equity": equity,
                "open_positions": sum(1 for s in holdings.values() if s > 0),
            }
        )

    equity_curve = pd.DataFrame(equity_rows)
    if not equity_curve.empty:
        equity_curve["date"] = pd.to_datetime(equity_curve["date"])
        equity_curve = equity_curve.set_index("date")

    final_prices = mark_prices(timeline[-1]) if timeline else {}
    final_hv = holdings_value(final_prices)
    final_equity = compute_portfolio_equity(cash, final_hv)

    per_ticker: dict[str, dict[str, Any]] = {}
    for ticker in tickers:
        ticker_trades = [t for t in trades if t.ticker == ticker]
        per_ticker[ticker] = {
            "buy_count": sum(1 for t in ticker_trades if t.side == "BUY"),
            "sell_count": sum(1 for t in ticker_trades if t.side == "SELL"),
            "final_shares": holdings.get(ticker, 0),
            "in_position": holdings.get(ticker, 0) > 0,
        }

    return Top3BacktestResult(
        initial_cash=initial_cash,
        final_equity=final_equity,
        total_return_pct=(final_equity / initial_cash - 1.0) * 100.0,
        max_drawdown_pct=compute_max_drawdown(equity_curve["equity"])
        if not equity_curve.empty
        else 0.0,
        sharpe_ratio=compute_sharpe_ratio(equity_curve["equity"])
        if not equity_curve.empty
        else 0.0,
        total_trades=len(trades),
        winning_trades=sum(1 for p in closed_pnls if p > 0),
        equity_curve=equity_curve,
        trades=trades,
        per_ticker_summary=per_ticker,
        rebalance_count=rebalance_count,
    )


def print_strategy_comparison_table(
    legacy: Any,
    top3: Top3BacktestResult,
    *,
    window_label: str = "",
) -> None:
    """Print side-by-side legacy vs Top3 metrics."""
    width = 72
    title = "STRATEGY COMPARISON: LEGACY vs TOP3"
    if window_label:
        title = f"{title} ({window_label})"
    print("=" * width)
    print(title.center(width))
    print("=" * width)
    print(f"{'Metric':<22} {'Legacy':>14} {'Top3':>14} {'Delta':>14}")
    print("-" * width)
    rows = [
        ("Total Return %", legacy.total_return_pct, top3.total_return_pct),
        ("Max Drawdown %", legacy.max_drawdown_pct, top3.max_drawdown_pct),
        ("Sharpe", legacy.sharpe_ratio, top3.sharpe_ratio),
        ("Total Trades", float(legacy.total_trades), float(top3.total_trades)),
        ("Round-Trip Wins", float(legacy.winning_trades), float(top3.winning_trades)),
        ("Final Equity $", legacy.final_equity, top3.final_equity),
    ]
    for label, leg, t3 in rows:
        if "Equity" in label:
            print(f"{label:<22} ${leg:>12,.2f} ${t3:>12,.2f} ${t3 - leg:>+12,.2f}")
        elif label in {"Total Trades", "Round-Trip Wins"}:
            print(f"{label:<22} {int(leg):>14} {int(t3):>14} {int(t3 - leg):>+14}")
        else:
            print(f"{label:<22} {leg:>+13.2f} {t3:>+13.2f} {t3 - leg:>+13.2f}")
    print("=" * width)
    winner = "Top3" if top3.total_return_pct > legacy.total_return_pct else "Legacy"
    if math.isclose(top3.total_return_pct, legacy.total_return_pct, abs_tol=0.01):
        winner = "Tie"
    print(f"Return winner: {winner} | Top3 rebalances: {top3.rebalance_count}")
