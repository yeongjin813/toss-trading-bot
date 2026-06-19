"""
Consolidated multi-ticker portfolio backtest engine.

Simulates NVDA / PLTR / AAPL (or any watchlist) sharing a single cash ledger
with live-parity dual-clamp sizing, entry filters, regime stops, and scale-in/out.
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
    build_spy_regime_lookup,
)
from config import StrategyConfig, StrategyConfigMapper
from entry_filters import passes_entry_filters
from market_registry import BENCHMARK_SMA_PERIOD
from momentum_ranker import (
    MomentumRankSettings,
    is_new_buy_allowed,
    rank_universe_frames,
    select_top_tickers,
    select_top_tickers_diversified,
    should_rebalance_today,
)
from trading_features import (
    TradingFeatureFlags,
    build_regime_lookup,
    build_spy_atr_pct_lookup,
    effective_risk_per_trade,
    resolve_bar_regime,
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
    from analytics import calculate_position_size

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
    for shares, price in holdings.values():
        equity += shares * price
    return equity


def compute_max_drawdown(equity_curve: pd.Series) -> float:
    if equity_curve.empty:
        return 0.0
    peak = equity_curve.cummax()
    drawdown = (equity_curve - peak) / peak.replace(0, math.nan)
    return float(abs(drawdown.min()) * 100.0)


def compute_sharpe_ratio(
    equity_curve: pd.Series,
    *,
    periods_per_year: int = 252,
) -> float:
    if len(equity_curve) < 2:
        return 0.0
    returns = equity_curve.pct_change().dropna()
    if returns.empty or returns.std() == 0:
        return 0.0
    return float((returns.mean() / returns.std()) * math.sqrt(periods_per_year))


def _prepare_ticker_series(ticker: str, raw: pd.DataFrame) -> TickerBacktestSeries:
    engine = LiveSignalEngine(ticker)
    config = engine.config
    frame = raw.copy()
    if "Date" not in frame.columns:
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index()
        if frame.columns[0] != "Date":
            frame = frame.rename(columns={frame.columns[0]: "Date"})
    frame["Date"] = pd.to_datetime(frame["Date"])
    enriched = engine.enrich(frame)
    date_to_index: dict[str, int] = {}
    for pos in range(len(enriched)):
        bar_date = pd.Timestamp(enriched.iloc[pos]["Date"]).strftime("%Y-%m-%d")
        date_to_index[bar_date] = pos
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
    *,
    scale_in_target: int = 0,
) -> None:
    series.shares = shares
    series.state.in_position = True
    series.state.held_quantity = shares
    series.state.highest_price_achieved = bar.close
    if series.state.entry_price is None:
        series.state.entry_price = bar.close
    if series.state.entry_bar_date is None:
        series.state.entry_bar_date = bar_date
        series.state.bars_held = 0
        series.state.hold_count_bar_date = bar_date
    series.state.days_below_sma_long = 0
    series.state.profit_trail_armed = False
    if scale_in_target > shares:
        series.state.scale_in_target_qty = scale_in_target
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
    series.state.partial_profit_taken = False
    series.state.scale_in_target_qty = 0
    series.state.last_processed_date = bar_date


def _apply_partial_sell_state(
    series: TickerBacktestSeries,
    sold_shares: int,
    bar_date: str,
) -> None:
    remaining = max(series.shares - sold_shares, 0)
    series.shares = remaining
    series.state.held_quantity = remaining
    series.state.in_position = remaining > 0
    series.state.partial_profit_taken = True
    series.state.last_processed_date = bar_date
    if remaining <= 0:
        _apply_sell_state(series, bar_date)


def _scale_in_eligible(
    series: TickerBacktestSeries,
    bar: BarSnapshot,
    *,
    market_bullish: bool,
) -> bool:
    target = int(series.state.scale_in_target_qty or 0)
    held = int(series.shares or 0)
    if target <= held or held <= 0:
        return False
    if int(series.state.bars_held or 0) < 3:
        return False
    if bar.close <= bar.sma_long:
        return False
    return market_bullish


class PortfolioBacktestEngine:
    """
    Multi-ticker consolidated portfolio simulator with live-parity gates.
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
        features: TradingFeatureFlags | None = None,
    ) -> None:
        self.initial_cash = initial_cash
        self.base_risk_per_trade = risk_per_trade
        self.commission_rate = commission_rate
        self.watchlist = tickers
        self.momentum_settings = momentum_settings or MomentumRankSettings.from_env()
        self.features = features or TradingFeatureFlags.from_env()
        self.use_spy_market_filter = (
            StrategyConfigMapper.use_spy_market_filter()
            if use_spy_market_filter is None
            else use_spy_market_filter
        )
        self.spy_lookup: dict[str, bool] | None = None
        self.regime_lookup = None
        self.spy_atr_lookup: dict[str, float] | None = None
        if self.use_spy_market_filter and spy_df is not None and not spy_df.empty:
            self.regime_lookup = build_regime_lookup(spy_df, qqq_df, features=self.features)
            if self.regime_lookup is None:
                self.spy_lookup = build_spy_regime_lookup(spy_df, BENCHMARK_SMA_PERIOD)
            self.spy_atr_lookup = build_spy_atr_pct_lookup(spy_df)
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
        if cfg.sector_diversify:
            active = select_top_tickers_diversified(
                ranked,
                top_n=cfg.top_n,
                max_per_sector=cfg.max_per_sector,
            )
        else:
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

    def _regime_context(self, bar_date: str) -> tuple[bool, float, float]:
        if not self.use_spy_market_filter:
            return True, 1.0, 1.0
        regime = resolve_bar_regime(
            bar_date,
            regime_lookup=self.regime_lookup,
            spy_lookup=self.spy_lookup,
        )
        return (
            regime.allow_new_buys,
            regime.position_size_multiplier,
            regime.atr_stop_multiplier,
        )

    def _entry_filters_ok(self, series: TickerBacktestSeries, bar_date: str) -> bool:
        ok, _ = passes_entry_filters(
            self._raw_frames[series.ticker],
            bar_date,
            settings=self.features.entry_filter_settings(),
        )
        return ok

    def _size_entry_shares(
        self,
        series: TickerBacktestSeries,
        bar: BarSnapshot,
        *,
        cash: float,
        total_equity: float,
        size_multiplier: float,
        bar_date: str,
        shares_hint: int | None = None,
    ) -> tuple[int, int]:
        risk = effective_risk_per_trade(
            self.base_risk_per_trade,
            bar_date,
            spy_atr_lookup=self.spy_atr_lookup,
            features=self.features,
        )
        stop_distance = bar.atr * series.config.atr_multiplier
        full_shares = shares_hint or dual_clamp_portfolio_size(
            total_equity=total_equity,
            available_cash=cash,
            entry_price=bar.close,
            stop_distance=stop_distance,
            risk_per_trade=risk,
            commission_rate=self.commission_rate,
        )
        if full_shares <= 0:
            return 0, 0
        if size_multiplier < 1.0:
            full_shares = max(1, int(full_shares * size_multiplier))
        if self.features.use_scale_in and full_shares > 1 and shares_hint is None:
            return max(1, full_shares // 2), full_shares
        return full_shares, 0

    def run(self) -> PortfolioBacktestResult:
        cash = self.initial_cash
        trades: list[TradeRecord] = []
        equity_rows: list[dict[str, Any]] = []

        closed_trade_pnls: list[float] = []
        open_entry_cost: dict[str, float] = {}

        for bar_date in self.timeline:
            self._maybe_rebalance_momentum(bar_date)
            market_bullish, size_multiplier, atr_stop_multiplier = self._regime_context(
                bar_date
            )

            exit_events: list[tuple[TickerBacktestSeries, BarSnapshot, str, str, int | None]] = []
            entry_events: list[tuple[TickerBacktestSeries, BarSnapshot, float, int | None]] = []
            scale_in_events: list[tuple[TickerBacktestSeries, BarSnapshot, int]] = []

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
                        atr_regime_multiplier=atr_stop_multiplier,
                        enable_scale_out=self.features.use_scale_out,
                    )
                    signal = exit_check["signal"]
                    if signal == "PARTIAL_SELL":
                        sell_qty = int(
                            exit_check.get("sell_quantity")
                            or max(1, series.shares // 3)
                        )
                        exit_events.append(
                            (
                                series,
                                bar,
                                signal,
                                str(exit_check.get("exit_reason") or signal),
                                sell_qty,
                            )
                        )
                    elif signal in {"SELL", "DYNAMIC_ATR_SELL"}:
                        exit_events.append(
                            (
                                series,
                                bar,
                                signal,
                                str(exit_check.get("exit_reason") or signal),
                                None,
                            )
                        )
                    elif _scale_in_eligible(
                        series,
                        bar,
                        market_bullish=market_bullish,
                    ):
                        add_qty = int(series.state.scale_in_target_qty) - series.shares
                        if add_qty > 0:
                            scale_in_events.append((series, bar, add_qty))
                else:
                    if not market_bullish or size_multiplier <= 0:
                        continue
                    entry_check = series.engine.evaluate_bar(
                        series.state,
                        bar,
                        prev_bar,
                        mutate_state=False,
                        allow_crossover=True,
                        market_bullish=market_bullish,
                    )
                    if entry_check["signal"] != "BUY":
                        continue
                    if not is_new_buy_allowed(
                        ticker,
                        self._active_trade_tickers,
                        settings=self.momentum_settings,
                    ):
                        continue
                    if not self._entry_filters_ok(series, bar_date):
                        continue
                    entry_events.append((series, bar, size_multiplier, None))

            for series, bar, signal, exit_reason, partial_qty in exit_events:
                if series.shares <= 0:
                    continue
                shares = partial_qty if partial_qty else series.shares
                shares = min(shares, series.shares)
                if shares <= 0:
                    continue
                gross = shares * bar.close
                commission = gross * self.commission_rate
                proceeds = gross - commission
                entry_cost = open_entry_cost.get(series.ticker, shares * bar.close)
                if partial_qty:
                    cost_basis = entry_cost * (shares / series.shares)
                    closed_trade_pnls.append(proceeds - cost_basis)
                    open_entry_cost[series.ticker] = entry_cost - cost_basis
                    _apply_partial_sell_state(series, shares, bar_date)
                else:
                    closed_trade_pnls.append(proceeds - entry_cost)
                    open_entry_cost.pop(series.ticker, None)
                    _apply_sell_state(series, bar_date)
                cash += proceeds
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

            for series, bar, size_multiplier, _ in entry_events:
                if series.shares > 0:
                    continue
                mark_prices = self._mark_prices(bar_date)
                holdings = self._holdings_snapshot(mark_prices)
                total_equity = compute_portfolio_equity(cash, holdings)
                shares, scale_target = self._size_entry_shares(
                    series,
                    bar,
                    cash=cash,
                    total_equity=total_equity,
                    size_multiplier=size_multiplier,
                    bar_date=bar_date,
                )
                if shares <= 0:
                    continue
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
                _apply_buy_state(
                    series,
                    bar,
                    shares,
                    bar_date,
                    scale_in_target=scale_target,
                )
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

            for series, bar, add_qty in scale_in_events:
                if series.shares <= 0 or add_qty <= 0:
                    continue
                mark_prices = self._mark_prices(bar_date)
                holdings = self._holdings_snapshot(mark_prices)
                total_equity = compute_portfolio_equity(cash, holdings)
                shares, _ = self._size_entry_shares(
                    series,
                    bar,
                    cash=cash,
                    total_equity=total_equity,
                    size_multiplier=1.0,
                    bar_date=bar_date,
                    shares_hint=add_qty,
                )
                shares = min(shares, add_qty)
                if shares <= 0:
                    continue
                gross = shares * bar.close
                commission = gross * self.commission_rate
                total_cost = gross + commission
                if total_cost > cash:
                    continue
                prior_cost = open_entry_cost.get(series.ticker, 0.0)
                cash -= total_cost
                new_total = series.shares + shares
                _apply_buy_state(series, bar, new_total, bar_date)
                series.state.scale_in_target_qty = 0
                open_entry_cost[series.ticker] = prior_cost + total_cost
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
                        signal="SCALE_IN_BUY",
                    )
                )

            mark_prices = self._mark_prices(bar_date)
            holdings = self._holdings_snapshot(mark_prices)
            equity = compute_portfolio_equity(cash, holdings)
            equity_rows.append({"date": bar_date, "equity": equity, "cash": cash})

        final_mark = self._mark_prices(self.timeline[-1]) if self.timeline else {}
        final_holdings = self._holdings_snapshot(final_mark)
        final_equity = compute_portfolio_equity(cash, final_holdings)

        equity_series = pd.Series(
            [row["equity"] for row in equity_rows],
            index=pd.to_datetime([row["date"] for row in equity_rows]),
        )
        exit_reason_counts: dict[str, int] = {}
        for trade in trades:
            if trade.side == "SELL" and trade.exit_reason:
                exit_reason_counts[trade.exit_reason] = (
                    exit_reason_counts.get(trade.exit_reason, 0) + 1
                )

        per_ticker: dict[str, dict[str, Any]] = {}
        for ticker, series in self.series_map.items():
            ticker_trades = [t for t in trades if t.ticker == ticker]
            buys = sum(1 for t in ticker_trades if t.side == "BUY")
            sells = sum(1 for t in ticker_trades if t.side == "SELL")
            per_ticker[ticker] = {
                "buy_count": buys,
                "sell_count": sells,
                "final_shares": series.shares,
                "in_position": series.shares > 0,
                "trades": len(ticker_trades),
            }

        winning = sum(1 for pnl in closed_trade_pnls if pnl > 0)
        total_return = (
            ((final_equity / self.initial_cash) - 1.0) * 100.0
            if self.initial_cash > 0
            else 0.0
        )

        equity_frame = pd.DataFrame(equity_rows)
        if not equity_frame.empty:
            equity_frame["date"] = pd.to_datetime(equity_frame["date"])
            equity_frame = equity_frame.set_index("date")

        return PortfolioBacktestResult(
            initial_cash=self.initial_cash,
            final_equity=final_equity,
            total_return_pct=total_return,
            max_drawdown_pct=compute_max_drawdown(equity_series),
            sharpe_ratio=compute_sharpe_ratio(equity_series),
            total_trades=len(trades),
            winning_trades=winning,
            equity_curve=equity_frame,
            trades=trades,
            per_ticker_summary=per_ticker,
            exit_reason_counts=exit_reason_counts,
        )


def run_portfolio_backtest(
    tickers: list[str],
    ohlcv_by_ticker: dict[str, pd.DataFrame],
    *,
    initial_cash: float = 10_000.0,
    risk_per_trade: float = 0.01,
    commission_rate: float = 0.001,
    use_spy_market_filter: bool | None = None,
    spy_df: pd.DataFrame | None = None,
    qqq_df: pd.DataFrame | None = None,
    momentum_settings: MomentumRankSettings | None = None,
    features: TradingFeatureFlags | None = None,
) -> PortfolioBacktestResult:
    engine = PortfolioBacktestEngine(
        tickers,
        ohlcv_by_ticker,
        initial_cash=initial_cash,
        risk_per_trade=risk_per_trade,
        commission_rate=commission_rate,
        use_spy_market_filter=use_spy_market_filter,
        spy_df=spy_df,
        qqq_df=qqq_df,
        momentum_settings=momentum_settings,
        features=features,
    )
    return engine.run()
