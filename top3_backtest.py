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
    select_top_tickers_diversified,
    should_rebalance_on_bar_date,
)
from momentum_selection import (
    summarize_top3_analytics,
    target_allocation_weights,
    target_set_unchanged,
    vol_map_from_ranked,
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
    closed_pnls: list[float] = field(default_factory=list)
    ranking_mode: str = "legacy"


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
    cfg = (momentum_settings or MomentumRankSettings.from_env()).for_top3()

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
    last_target: list[str] = []
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
            if cfg.sector_diversify:
                target = select_top_tickers_diversified(
                    ranked,
                    top_n=cfg.top_n,
                    max_per_sector=cfg.max_per_sector,
                )
            else:
                target = select_top_tickers(ranked, top_n=cfg.top_n)
            if not target:
                target = tickers[: cfg.top_n]

            skip_trades = (
                not first_run
                and cfg.ranking_mode == "enhanced"
                and cfg.dynamic_rebalance_only
                and last_target
                and target_set_unchanged(last_target, target)
            )

            if not skip_trades:
                prices = mark_prices(bar_date)
                hv = holdings_value(prices)
                equity = compute_portfolio_equity(cash, hv)

                for ticker, shares in list(holdings.items()):
                    if shares > 0 and ticker not in target and ticker in prices:
                        do_sell(ticker, shares, prices[ticker], bar_date, "TOP3_EXIT")

                prices = mark_prices(bar_date)
                hv = holdings_value(prices)
                equity = compute_portfolio_equity(cash, hv)
                use_inverse_vol = cfg.ranking_mode == "enhanced" and cfg.inverse_vol_weighting
                weights = target_allocation_weights(
                    target,
                    vol_map_from_ranked(ranked, target),
                    use_inverse_vol=use_inverse_vol,
                )

                for ticker in target:
                    if ticker not in prices or prices[ticker] <= 0:
                        continue
                    price = prices[ticker]
                    current_shares = holdings.get(ticker, 0)
                    slot_equity = equity * weights.get(ticker, 0.0)
                    target_shares = int(slot_equity / price)
                    delta = target_shares - current_shares
                    if delta < 0:
                        do_sell(ticker, -delta, price, bar_date, "TOP3_TRIM")
                    elif delta > 0:
                        do_buy(ticker, delta, price, bar_date)

                rebalance_count += 1

            last_target = list(target)
            last_rebalance = bar_date

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
        closed_pnls=closed_pnls,
        ranking_mode=cfg.ranking_mode,
    )


def analytics_for_top3_result(result: Top3BacktestResult) -> dict[str, float | int | dict[str, float]]:
    """Extended metrics for legacy vs enhanced comparison."""
    metrics = summarize_top3_analytics(
        initial_cash=result.initial_cash,
        final_equity=result.final_equity,
        equity_curve=result.equity_curve,
        closed_pnls=result.closed_pnls,
        total_trades=result.total_trades,
        winning_trades=result.winning_trades,
    )
    metrics["rebalance_count"] = result.rebalance_count
    metrics["ranking_mode"] = result.ranking_mode
    return metrics


def _normalized_equity_series(result: Any) -> pd.Series:
    if result.equity_curve.empty:
        return pd.Series(dtype=float)
    frame = result.equity_curve
    if "equity" not in frame.columns:
        return pd.Series(dtype=float)
    if isinstance(frame.index, pd.DatetimeIndex):
        return frame["equity"].astype(float)
    if "date" in frame.columns:
        idx = pd.to_datetime(frame["date"])
        return pd.Series(frame["equity"].astype(float).values, index=idx)
    return frame["equity"].astype(float)


def combine_dual_equity_curves(
    legacy: Any,
    top3: Top3BacktestResult,
) -> pd.Series:
    """Sum legacy + Top3 daily equity (separate capital pools, shared calendar)."""
    leg = _normalized_equity_series(legacy)
    t3 = _normalized_equity_series(top3)
    if leg.empty and t3.empty:
        return pd.Series(dtype=float)
    combined = pd.concat([leg.rename("legacy"), t3.rename("top3")], axis=1).sort_index()
    combined = combined.ffill()
    combined = combined.fillna(
        {"legacy": legacy.initial_cash, "top3": top3.initial_cash}
    )
    return combined.sum(axis=1)


def summarize_dual_combined(
    legacy: Any,
    top3: Top3BacktestResult,
    *,
    legacy_pct: float = 60.0,
    top3_pct: float = 40.0,
) -> dict[str, float | int]:
    """Metrics for Phase 4 split: legacy pool + Top3 pool on one $100k account."""
    total_initial = legacy.initial_cash + top3.initial_cash
    total_final = legacy.final_equity + top3.final_equity
    combined_equity = combine_dual_equity_curves(legacy, top3)
    return {
        "legacy_pct": legacy_pct,
        "top3_pct": top3_pct,
        "initial_cash": total_initial,
        "final_equity": total_final,
        "total_return_pct": (
            (total_final - total_initial) / total_initial * 100.0
            if total_initial > 0
            else 0.0
        ),
        "max_drawdown_pct": compute_max_drawdown(combined_equity)
        if not combined_equity.empty
        else 0.0,
        "sharpe_ratio": compute_sharpe_ratio(combined_equity)
        if not combined_equity.empty
        else 0.0,
        "total_trades": legacy.total_trades + top3.total_trades,
    }


def print_dual_combined_summary(
    legacy: Any,
    top3: Top3BacktestResult,
    *,
    legacy_pct: float = 60.0,
    top3_pct: float = 40.0,
    window_label: str = "",
) -> None:
    """Print Phase 4 combined ledger (legacy slice + Top3 slice)."""
    metrics = summarize_dual_combined(
        legacy, top3, legacy_pct=legacy_pct, top3_pct=top3_pct
    )
    width = 88
    title = f"PHASE 4 DUAL COMBINED ({legacy_pct:.0f}/{top3_pct:.0f})"
    if window_label:
        title = f"{title} - {window_label}"
    print("=" * width)
    print(title.center(width))
    print("=" * width)
    print(f"Legacy pool          : ${legacy.initial_cash:,.2f} -> ${legacy.final_equity:,.2f} ({legacy.total_return_pct:+.2f}%)")
    print(f"Top3 pool            : ${top3.initial_cash:,.2f} -> ${top3.final_equity:,.2f} ({top3.total_return_pct:+.2f}%)")
    print(f"Combined initial     : ${metrics['initial_cash']:,.2f}")
    print(f"Combined final       : ${metrics['final_equity']:,.2f}")
    print(f"Combined return      : {metrics['total_return_pct']:+.2f}%")
    print(f"Combined MaxDD       : {metrics['max_drawdown_pct']:.2f}%")
    print(f"Combined Sharpe      : {metrics['sharpe_ratio']:.2f}")
    print(f"Combined trades      : {metrics['total_trades']}")
    print("=" * width)


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
