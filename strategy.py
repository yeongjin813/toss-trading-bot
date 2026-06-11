from __future__ import annotations

import os

import backtrader as bt

from config import StrategyConfig, StrategyConfigMapper


class TrendTradingStrategy(bt.Strategy):
    """
    Ticker-bound Backtrader twin of analytics.LiveSignalEngine.

    Initialization binds StrategyConfigMapper.for_ticker(ticker) and executes
    conditional regime filtering with asymmetric whipsaw protection.
    """

    params = (
        ("ticker", "DEFAULT"),
        ("sma_period", 10),
        ("sma_long_period", 50),
        ("rsi_period", 14),
        ("rsi_buy_threshold", 50.0),
        ("rsi_exit_threshold", 50.0),
        ("atr_period", 14),
        ("atr_multiplier", 2.0),
        ("volume_sma_period", 20),
        ("volume_threshold", 1.2),
        ("use_trend_filter", True),
        ("use_trailing_stop", True),
    )

    @classmethod
    def params_from_ticker(cls, ticker: str) -> tuple:
        """Map isolated StrategyConfig into Backtrader strategy params."""
        cfg = StrategyConfigMapper.for_ticker(ticker)
        return (
            ("ticker", cfg.ticker),
            ("sma_period", cfg.sma_period),
            ("sma_long_period", cfg.sma_long_period),
            ("rsi_period", cfg.rsi_period),
            ("rsi_buy_threshold", cfg.rsi_buy_threshold),
            ("rsi_exit_threshold", cfg.rsi_exit_threshold),
            ("atr_period", cfg.atr_period),
            ("atr_multiplier", cfg.atr_multiplier),
            ("volume_sma_period", cfg.volume_sma_period),
            ("volume_threshold", cfg.volume_threshold),
            ("use_trend_filter", cfg.use_trend_filter),
            ("use_trailing_stop", cfg.use_trailing_stop),
        )

    def __init__(self) -> None:
        self.config = StrategyConfigMapper.for_ticker(self.params.ticker)

        self.sma_short = bt.indicators.SMA(
            self.data.close, period=self.params.sma_period
        )
        self.sma_long = bt.indicators.SMA(
            self.data.close, period=self.params.sma_long_period
        )
        self.crossover = bt.indicators.CrossOver(self.data.close, self.sma_short)
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)
        self.atr = bt.indicators.ATR(self.data, period=self.params.atr_period)
        self.volume_sma = bt.indicators.SMA(
            self.data.volume, period=self.params.volume_sma_period
        )

        self.highest_price_achieved: float | None = None
        self.trigger_floor: float | None = None
        self.order = None
        self._min_period = max(
            self.params.sma_period,
            self.params.sma_long_period,
            self.params.rsi_period,
            self.params.atr_period,
            self.params.volume_sma_period,
        )

    def notify_order(self, order: bt.Order) -> None:
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status in [
            order.Completed,
            order.Canceled,
            order.Margin,
            order.Rejected,
        ]:
            if order.status == order.Completed:
                if order.isbuy():
                    self.highest_price_achieved = order.executed.price
                    self.trigger_floor = (
                        self.highest_price_achieved - self._dynamic_stop_distance()
                    )
                elif order.issell() and not self.position:
                    self._reset_trailing_state()

            self.order = None

    def _reset_trailing_state(self) -> None:
        self.highest_price_achieved = None
        self.trigger_floor = None

    def _update_trailing_state(self, close: float) -> None:
        if self.highest_price_achieved is None or close > self.highest_price_achieved:
            self.highest_price_achieved = close
        self.trigger_floor = (
            self.highest_price_achieved - self._dynamic_stop_distance()
        )

    def _dynamic_stop_distance(self) -> float:
        return float(self.atr[0]) * self.params.atr_multiplier

    def _dynamic_atr_stop_triggered(self, bar_low: float) -> bool:
        if not self.params.use_trailing_stop or self.trigger_floor is None:
            return False
        return bar_low <= self.trigger_floor

    def _passes_volume_filter(self) -> bool:
        """Volume gate: Volume_t > volume_mult * Volume_SMA_t."""
        return float(self.data.volume[0]) > float(self.volume_sma[0]) * float(
            self.params.volume_threshold
        )

    def _rsi_allows_entry(self) -> bool:
        """RSI gate: RSI_t >= rsi_buy_threshold."""
        return float(self.rsi[0]) >= self.params.rsi_buy_threshold

    def _passes_trend_filter(self) -> bool:
        """
        Trend-filtered entry gate.

        When use_trend_filter=True, BUY is blocked unless:
          current_close > sma_long  OR  sma_short > sma_long

        When use_trend_filter=False (PLTR breakout regime), always passes.
        """
        if not self.params.use_trend_filter:
            return True

        current_close = float(self.data.close[0])
        sma_short = float(self.sma_short[0])
        sma_long = float(self.sma_long[0])
        return current_close > sma_long or sma_short > sma_long

    def _in_weak_regime(self) -> bool:
        """Documented weak regime: current_close < sma_long."""
        return float(self.data.close[0]) < float(self.sma_long[0])

    def _rsi_crossdown_exit(self) -> bool:
        """
        RSI crossdown through rsi_exit_threshold (50):
        RSI_{t-1} >= 50 AND RSI_t < 50.
        """
        threshold = self.params.rsi_exit_threshold
        return float(self.rsi[-1]) >= threshold and float(self.rsi[0]) < threshold

    def _rsi_exit_allowed(self) -> bool:
        """
        Asymmetric whipsaw protection for RSI exits.

        Eligible when:
          - use_trend_filter=False (PLTR): always
          - use_trend_filter=True  (NVDA/DEFAULT): only in weak regime
            (current_close < sma_long)

        When trading safely above sma_long, RSI crossdown is suppressed.
        """
        if not self.params.use_trend_filter:
            return True
        return self._in_weak_regime()

    def _death_cross(self) -> bool:
        """Unconditional emergency exit: Close crosses below sma_short."""
        return self.crossover[0] < 0

    def _evaluate_exit_signals(self) -> bool:
        """
        Exit decision tree (post-ATR-stop survival):
          1. Death cross — unconditional
          2. RSI crossdown — conditional on regime gate
        """
        if self._death_cross():
            return True
        return self._rsi_exit_allowed() and self._rsi_crossdown_exit()

    def _evaluate_entry_signal(self) -> bool:
        """
        Entry decision tree (flat position):
          Golden Cross AND RSI gate AND Volume gate AND Trend filter gate.
        """
        golden_cross = self.crossover[0] > 0
        return (
            golden_cross
            and self._rsi_allows_entry()
            and self._passes_volume_filter()
            and self._passes_trend_filter()
        )

    def next(self) -> None:
        if len(self) < self._min_period:
            return

        if self.order:
            return

        current_close = float(self.data.close[0])
        bar_low = float(self.data.low[0])

        if self.position:
            if self._dynamic_atr_stop_triggered(bar_low):
                self.order = self.close()
                return

            self._update_trailing_state(current_close)

            if self._evaluate_exit_signals():
                self.order = self.close()
                return
        elif self._evaluate_entry_signal():
            self.order = self.buy()


def default_capital_at_risk() -> float:
    return float(os.getenv("CAPITAL_AT_RISK", "10000"))


DEFAULT_COMMISSION_RATE = 0.001


def configure_backtest_broker(
    cerebro: bt.Cerebro,
    commission_rate: float = DEFAULT_COMMISSION_RATE,
) -> bt.Cerebro:
    """
    Apply broker settings aligned with the live EOD close execution model.

    Cheat-on-Close forces fills at the current bar's close (Close_t), matching
    analytics.py / main.py rather than Backtrader's default next-bar open.
    """
    cerebro.broker.setcommission(commission=commission_rate)
    cerebro.broker.set_coc(True)
    return cerebro


def create_backtest_cerebro(
    ticker: str,
    initial_cash: float | None = None,
    commission_rate: float = DEFAULT_COMMISSION_RATE,
) -> bt.Cerebro:
    """
    Initialize Backtrader engine with ticker-isolated TrendTradingStrategy.

    Usage:
        cerebro = create_backtest_cerebro("NVDA")
        cerebro.adddata(data_feed)
        cerebro.run()
    """
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(
        initial_cash if initial_cash is not None else default_capital_at_risk()
    )
    configure_backtest_broker(cerebro, commission_rate=commission_rate)

    strategy_params = dict(TrendTradingStrategy.params_from_ticker(ticker))
    cerebro.addstrategy(TrendTradingStrategy, **strategy_params)
    return cerebro


SmaCross = TrendTradingStrategy


def load_backtest_config(ticker: str = "DEFAULT") -> StrategyConfig:
    """Resolve ticker-specific StrategyConfig for backtest orchestration."""
    return StrategyConfigMapper.for_ticker(ticker)
