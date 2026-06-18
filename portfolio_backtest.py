"""
Consolidated multi-ticker portfolio backtest engine.

Simulates NVDA / PLTR / AAPL (or any watchlist) sharing a single cash ledger
with live-parity dual-clamp position sizing from analytics.calculate_position_size().
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from analytics import (
    BarSnapshot,
    LiveSignalEngine,
    PositionState,
    build_market_regime_lookup,
    build_spy_regime_lookup,
    calculate_position_size,
    resolve_market_regime,
    resolve_spy_market_bullish,
)
from config import StrategyConfig, StrategyConfigMapper
from market_registry import BENCHMARK_SMA_PERIOD, SECONDARY_BENCHMARK_TICKER
from momentum_ranker import (
    MomentumRankSettings,
    is_new_buy_allowed,
    rank_universe_frames,
    select_top_tickers,
    should_rebalance_today,
)


@dataclass
class TradeRecord:
    date: str
    ticker: str
    side: str
    shares: int
    price: float
    commission: float
    cash_after: float
    equity_after: float
    signal: str
    exit_reason: str = ""


@dataclass
class TickerBacktestSeries:
    ticker: str
    engine: LiveSignalEngine
    config: StrategyConfig
    enriched: pd.DataFrame
    date_to_index: dict[str, int]
    state: PositionState = field(default_factory=PositionState)
    shares: int = 0


@dataclass
class PortfolioBacktestResult:
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
    exit_reason_counts: dict[str, int] = field(default_factory=dict)


def dual_clamp_portfolio_size(
    total_equity: float,
    available_cash: float,
    entry_price: float,
    stop_distance: float,
    risk_per_trade: float = 0.01,
    commission_rate: float = 0.001,
) -> int:
    """
    Live-parity dual-clamp sizing bound to portfolio ledger.

    Risk clamp:    int((total_equity * risk_per_trade) / stop_distance)
    Capital clamp: int((available_cash * 0.95) / entry_price)
    Execution:     min(risk, capital), scaled down if commission exceeds cash.
    """
    if entry_price <= 0 or stop_distance <= 0 or available_cash <= 0:
        return 0

    shares = calculate_position_size(
        capital_at_risk=total_equity,
        risk_per_trade=risk_per_trade,
        entry_price=entry_price,
        stop_distance=stop_distance,
        available_capital=available_cash,
    )
    if shares <= 0:
        return 0

    cost_with_commission = shares * entry_price * (1.0 + commission_rate)
    if cost_with_commission > available_cash:
        affordable = int(available_cash / (entry_price * (1.0 + commission_rate)))
        if affordable <= 0:
            return 0
        shares = min(shares, affordable)

    return max(shares, 0)


def compute_portfolio_equity(
    cash: float,
    holdings: dict[str, tuple[int, float]],
) -> float:
    """Total equity = free cash + sum(shares * mark_price)."""
    equity = cash
    for _ticker, (shares, mark_price) in holdings.items():
        if shares > 0:
            equity += shares * mark_price
    return equity


def compute_max_drawdown(equity_series: pd.Series) -> float:
    """Peak-to-trough drawdown as a positive percentage."""
    if equity_series.empty:
        return 0.0
    running_peak = equity_series.cummax()
    drawdown = (equity_series - running_peak) / running_peak
    return float(abs(drawdown.min()) * 100.0)


def compute_sharpe_ratio(
    equity_series: pd.Series,
    trading_days: int = 252,
    risk_free_rate: float = 0.0,
) -> float:
    """Annualized Sharpe from daily equity returns."""
    if len(equity_series) < 2:
        return 0.0
    daily_returns = equity_series.pct_change().dropna()
    if daily_returns.empty or daily_returns.std() == 0:
        return 0.0
    excess = daily_returns - (risk_free_rate / trading_days)
    return float(excess.mean() / excess.std() * math.sqrt(trading_days))


def _prepare_ticker_series(ticker: str, ohlcv: pd.DataFrame) -> TickerBacktestSeries:
    engine = LiveSignalEngine(ticker)
    config = StrategyConfigMapper.for_ticker(ticker)
    raw = ohlcv.reset_index()
    if "Date" not in raw.columns:
        raw = raw.rename(columns={raw.columns[0]: "Date"})
    raw["Date"] = pd.to_datetime(raw["Date"])
    enriched = engine.enrich(raw)
    date_to_index = {
        pd.Timestamp(row["Date"]).strftime("%Y-%m-%d"): idx
        for idx, row in enriched.iterrows()
    }
    return TickerBacktestSeries(
        ticker=ticker,
        engine=engine,
        config=config,
        enriched=enriched,
        date_to_index=date_to_index,
    )


def _apply_buy_state(
    series: TickerBacktestSeries,
    bar: BarSnapshot,
    shares: int,
    bar_date: str,
) -> None:
    series.shares = shares
    series.state.in_position = True
    series.state.held_quantity = shares
    series.state.highest_price_achieved = bar.close
    series.state.entry_price = bar.close
    series.state.entry_bar_date = bar_date
    series.state.bars_held = 0
    series.state.hold_count_bar_date = bar_date
    series.state.days_below_sma_long = 0
    series.state.profit_trail_armed = False
    series.engine._update_trailing_state(series.state, bar.close, bar.atr)
    series.state.last_processed_date = bar_date


def _apply_sell_state(series: TickerBacktestSeries, bar_date: str) -> None:
    series.shares = 0
    series.state.in_position = False
    series.state.held_quantity = 0
    series.state.highest_price_achieved = None
    series.state.current_atr = None
    series.state.dynamic_stop_distance = None
    series.state.trigger_floor = None
    series.state.session_low = None
    series.state.entry_price = None
    series.state.entry_bar_date = None
    series.state.bars_held = 0
    series.state.hold_count_bar_date = None
    series.state.days_below_sma_long = 0
    series.state.profit_trail_armed = False
    series.state.last_processed_date = bar_date


def _update_in_position_trailing(
    series: TickerBacktestSeries,
    bar: BarSnapshot,
    prev_bar: BarSnapshot,
) -> None:
    """Advance trailing stop state on HOLD bars without triggering crossover exits."""
    if series.shares <= 0:
        return
    result = series.engine.evaluate_bar(
        series.state,
        bar,
        prev_bar,
        mutate_state=True,
        allow_crossover=False,
    )
    if result["signal"] in {"SELL", "DYNAMIC_ATR_SELL"}:
        raise RuntimeError(
            f"Unexpected exit signal during trailing update for {series.ticker}"
        )


class PortfolioBacktestEngine:
    """
    Multi-ticker consolidated portfolio simulator.

    - Single starting cash pool shared across all tickers
    - Per-ticker LiveSignalEngine + StrategyConfig isolation
    - Dual-clamp sizing on total equity with available-cash cap
    - Exits processed before entries each day (watchlist order)
    """

    def __init__(
        self,
        tickers: list[str],
        ohlcv_by_ticker: dict[str, pd.DataFrame],
        initial_cash: float = 10_000.0,
        risk_per_trade: float = 0.01,
        commission_rate: float = 0.001,
        use_spy_market_filter: bool | None = None,
        spy_df: pd.DataFrame | None = None,
        qqq_df: pd.DataFrame | None = None,
        momentum_settings: MomentumRankSettings | None = None,
    ) -> None:
        self.initial_cash = initial_cash
        self.risk_per_trade = risk_per_trade
        self.commission_rate = commission_rate
        self.watchlist = tickers
        self.momentum_settings = momentum_settings or MomentumRankSettings.from_env()
        self.use_spy_market_filter = (
            StrategyConfigMapper.use_spy_market_filter()
            if use_spy_market_filter is None
            else use_spy_market_filter
        )
        self.spy_lookup: dict[str, bool] | None = None
        self.regime_lookup = None
        if self.use_spy_market_filter and spy_df is not None and not spy_df.empty:
            if (
                StrategyConfigMapper.use_qqq_regime_filter()
                and qqq_df is not None
                and not qqq_df.empty
            ):
                self.regime_lookup = build_market_regime_lookup(
                    spy_df,
                    qqq_df,
                    sma_period=BENCHMARK_SMA_PERIOD,
                )
            else:
                self.spy_lookup = build_spy_regime_lookup(spy_df, BENCHMARK_SMA_PERIOD)
        self.series_map: dict[str, TickerBacktestSeries] = {}

        for ticker in tickers:
            if ticker not in ohlcv_by_ticker:
                raise ValueError(f"Missing OHLCV data for {ticker}")
            self.series_map[ticker] = _prepare_ticker_series(
                ticker, ohlcv_by_ticker[ticker]
            )

        all_dates: set[str] = set()
        for series in self.series_map.values():
            all_dates.update(series.date_to_index.keys())
        self.timeline = sorted(all_dates)
        self._active_trade_tickers: set[str] = set(tickers)
        self._momentum_last_rebalance: str | None = None
        self._raw_frames = {
            ticker: ohlcv_by_ticker[ticker].copy() for ticker in tickers
        }

    def _maybe_rebalance_momentum(self, bar_date: str) -> None:
        cfg = self.momentum_settings
        if not cfg.enabled:
            self._active_trade_tickers = set(self.watchlist)
            return

        bar_ts = pd.Timestamp(bar_date)
        pseudo_now = bar_ts.to_pydatetime()
        first_run = self._momentum_last_rebalance is None
        if not first_run and not should_rebalance_today(
            pseudo_now,
            self._momentum_last_rebalance,
            rebalance_weekday=cfg.rebalance_weekday,
        ):
            return

        ranked = rank_universe_frames(
            self._raw_frames,
            self.watchlist,
            as_of_date=bar_date,
            settings=cfg,
        )
        active = select_top_tickers(ranked, top_n=cfg.top_n)
        if active:
            self._active_trade_tickers = set(active)
        self._momentum_last_rebalance = bar_date

    def _mark_prices(self, bar_date: str) -> dict[str, float]:
        prices: dict[str, float] = {}
        for ticker, series in self.series_map.items():
            idx = series.date_to_index.get(bar_date)
            if idx is None:
                continue
            prices[ticker] = float(series.enriched.iloc[idx]["Close"])
        return prices

    def _holdings_snapshot(
        self, mark_prices: dict[str, float]
    ) -> dict[str, tuple[int, float]]:
        holdings: dict[str, tuple[int, float]] = {}
        for ticker, series in self.series_map.items():
            if series.shares > 0 and ticker in mark_prices:
                holdings[ticker] = (series.shares, mark_prices[ticker])
        return holdings

    def run(self) -> PortfolioBacktestResult:
        cash = self.initial_cash
        trades: list[TradeRecord] = []
        equity_rows: list[dict[str, Any]] = []

        closed_trade_pnls: list[float] = []
        open_entry_cost: dict[str, float] = {}

        for bar_date in self.timeline:
            self._maybe_rebalance_momentum(bar_date)
            mark_prices = self._mark_prices(bar_date)
            holdings = self._holdings_snapshot(mark_prices)

            exit_events: list[tuple[TickerBacktestSeries, BarSnapshot, str, str]] = []
            entry_events: list[tuple[TickerBacktestSeries, BarSnapshot, float]] = []

            for ticker in self.watchlist:
                series = self.series_map[ticker]
                idx = series.date_to_index.get(bar_date)
                if idx is None or idx < 1:
                    continue

                bar = BarSnapshot.from_row(series.enriched.iloc[idx])
                prev_bar = BarSnapshot.from_row(series.enriched.iloc[idx - 1])

                if series.shares > 0:
                    ranked_hold = ticker in self._active_trade_tickers
                    exit_check = series.engine.evaluate_bar(
                        series.state,
                        bar,
                        prev_bar,
                        mutate_state=True,
                        allow_crossover=True,
                        momentum_ranked_hold=ranked_hold,
                    )
                    if exit_check["signal"] in {"SELL", "DYNAMIC_ATR_SELL"}:
                        exit_events.append(
                            (
                                series,
                                bar,
                                exit_check["signal"],
                                str(exit_check.get("exit_reason") or exit_check["signal"]),
                            )
                        )
                else:
                    if self.regime_lookup is not None:
                        regime = resolve_market_regime(self.regime_lookup, bar_date)
                        market_bullish = regime.allow_new_buys
                        size_multiplier = regime.position_size_multiplier
                    else:
                        market_bullish = resolve_spy_market_bullish(
                            self.spy_lookup,
                            bar_date,
                        )
                        size_multiplier = 1.0 if market_bullish else 0.0
                    entry_check = series.engine.evaluate_bar(
                        series.state,
                        bar,
                        prev_bar,
                        mutate_state=False,
                        allow_crossover=True,
                        market_bullish=market_bullish,
                    )
                    if entry_check["signal"] == "BUY" and is_new_buy_allowed(
                        ticker,
                        self._active_trade_tickers,
                        settings=self.momentum_settings,
                    ):
                        entry_events.append((series, bar, size_multiplier))

            for series, bar, signal, exit_reason in exit_events:
                if series.shares <= 0:
                    continue
                shares = series.shares
                gross = shares * bar.close
                commission = gross * self.commission_rate
                proceeds = gross - commission
                entry_cost = open_entry_cost.pop(series.ticker, shares * bar.close)
                closed_trade_pnls.append(proceeds - entry_cost)
                cash += proceeds
                _apply_sell_state(series, bar_date)
                mark_prices = self._mark_prices(bar_date)
                holdings = self._holdings_snapshot(mark_prices)
                equity = compute_portfolio_equity(cash, holdings)
                trades.append(
                    TradeRecord(
                        date=bar_date,
                        ticker=series.ticker,
                        side="SELL",
                        shares=shares,
                        price=bar.close,
                        commission=commission,
                        cash_after=cash,
                        equity_after=equity,
                        signal=signal,
                        exit_reason=exit_reason,
                    )
                )

            for series, bar, size_multiplier in entry_events:
                if series.shares > 0:
                    continue

                if size_multiplier <= 0:
                    series.engine._clear_position_fields(series.state)
                    continue

                mark_prices = self._mark_prices(bar_date)
                holdings = self._holdings_snapshot(mark_prices)
                total_equity = compute_portfolio_equity(cash, holdings)
                stop_distance = bar.atr * series.config.atr_multiplier

                shares = dual_clamp_portfolio_size(
                    total_equity=total_equity,
                    available_cash=cash,
                    entry_price=bar.close,
                    stop_distance=stop_distance,
                    risk_per_trade=self.risk_per_trade,
                    commission_rate=self.commission_rate,
                )
                if shares <= 0:
                    continue
                if size_multiplier < 1.0:
                    shares = max(1, int(shares * size_multiplier))

                gross = shares * bar.close
                commission = gross * self.commission_rate
                total_cost = gross + commission
                if total_cost > cash:
                    shares = int(cash / (bar.close * (1.0 + self.commission_rate)))
                    if shares <= 0:
                        continue
                    gross = shares * bar.close
                    commission = gross * self.commission_rate
                    total_cost = gross + commission

                cash -= total_cost
                _apply_buy_state(series, bar, shares, bar_date)
                open_entry_cost[series.ticker] = total_cost
                mark_prices = self._mark_prices(bar_date)
                holdings = self._holdings_snapshot(mark_prices)
                equity = compute_portfolio_equity(cash, holdings)
                trades.append(
                    TradeRecord(
                        date=bar_date,
                        ticker=series.ticker,
                        side="BUY",
                        shares=shares,
                        price=bar.close,
                        commission=commission,
                        cash_after=cash,
                        equity_after=equity,
                        signal="BUY",
                    )
                )

            mark_prices = self._mark_prices(bar_date)
            holdings = self._holdings_snapshot(mark_prices)
            equity = compute_portfolio_equity(cash, holdings)
            equity_rows.append(
                {
                    "date": bar_date,
                    "cash": cash,
                    "equity": equity,
                    "open_positions": sum(1 for s in self.series_map.values() if s.shares > 0),
                }
            )

        equity_curve = pd.DataFrame(equity_rows)
        if not equity_curve.empty:
            equity_curve["date"] = pd.to_datetime(equity_curve["date"])
            equity_curve = equity_curve.set_index("date")

        final_mark = self._mark_prices(self.timeline[-1]) if self.timeline else {}
        final_holdings = self._holdings_snapshot(final_mark)
        final_equity = compute_portfolio_equity(cash, final_holdings)

        winning_trades = sum(1 for pnl in closed_trade_pnls if pnl > 0)
        per_ticker = self._build_per_ticker_summary(trades)
        exit_reason_counts: dict[str, int] = {}
        for trade in trades:
            if trade.side == "SELL" and trade.exit_reason:
                exit_reason_counts[trade.exit_reason] = (
                    exit_reason_counts.get(trade.exit_reason, 0) + 1
                )

        return PortfolioBacktestResult(
            initial_cash=self.initial_cash,
            final_equity=final_equity,
            total_return_pct=(final_equity / self.initial_cash - 1.0) * 100.0,
            max_drawdown_pct=compute_max_drawdown(equity_curve["equity"])
            if not equity_curve.empty
            else 0.0,
            sharpe_ratio=compute_sharpe_ratio(equity_curve["equity"])
            if not equity_curve.empty
            else 0.0,
            total_trades=len(trades),
            winning_trades=winning_trades,
            equity_curve=equity_curve,
            trades=trades,
            per_ticker_summary=per_ticker,
            exit_reason_counts=exit_reason_counts,
        )

    def _build_per_ticker_summary(
        self, trades: list[TradeRecord]
    ) -> dict[str, dict[str, Any]]:
        summary: dict[str, dict[str, Any]] = {}
        for ticker in self.watchlist:
            ticker_trades = [t for t in trades if t.ticker == ticker]
            buys = [t for t in ticker_trades if t.side == "BUY"]
            sells = [t for t in ticker_trades if t.side == "SELL"]
            summary[ticker] = {
                "buy_count": len(buys),
                "sell_count": len(sells),
                "final_shares": self.series_map[ticker].shares,
                "in_position": self.series_map[ticker].shares > 0,
            }
        return summary


def run_portfolio_backtest(
    tickers: list[str],
    ohlcv_by_ticker: dict[str, pd.DataFrame],
    initial_cash: float = 10_000.0,
    risk_per_trade: float = 0.01,
    commission_rate: float = 0.001,
    use_spy_market_filter: bool | None = None,
    spy_df: pd.DataFrame | None = None,
    qqq_df: pd.DataFrame | None = None,
    momentum_settings: MomentumRankSettings | None = None,
) -> PortfolioBacktestResult:
    """Convenience wrapper for consolidated portfolio simulation."""
    engine = PortfolioBacktestEngine(
        tickers=tickers,
        ohlcv_by_ticker=ohlcv_by_ticker,
        initial_cash=initial_cash,
        risk_per_trade=risk_per_trade,
        commission_rate=commission_rate,
        use_spy_market_filter=use_spy_market_filter,
        spy_df=spy_df,
        qqq_df=qqq_df,
        momentum_settings=momentum_settings,
    )
    return engine.run()
