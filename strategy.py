from __future__ import annotations

import os

import backtrader as bt

from analytics import StrategyConfig


def load_backtest_config() -> StrategyConfig:
    """Load backtest parameters aligned with live STRATEGY_CONFIG / .env overrides."""
    return StrategyConfig.from_env()


class SmaCross(bt.Strategy):
    """SMA crossover strategy with RSI filter, volume gate, and dynamic ATR trailing stop."""

    params = (
        ("sma_period", 10),
        ("rsi_period", 14),
        ("rsi_buy_threshold", 50),
        ("rsi_sell_threshold", 70),
        ("rsi_exit_mode", "crossdown"),
        ("atr_period", 14),
        ("atr_multiplier", 2.0),
        ("volume_sma_period", 20),
        ("volume_threshold", 1.2),
        ("use_rsi_filter", True),
        ("use_volume_filter", True),
        ("use_trailing_stop", True),
    )

    @classmethod
    def params_from_config(cls, config: StrategyConfig | None = None) -> tuple:
        cfg = config or load_backtest_config()
        return (
            ("sma_period", cfg.sma_period),
            ("rsi_period", cfg.rsi_period),
            ("rsi_buy_threshold", cfg.rsi_buy_threshold),
            ("rsi_sell_threshold", cfg.rsi_upper_limit),
            ("rsi_exit_mode", cfg.rsi_exit_mode),
            ("atr_period", cfg.atr_period),
            ("atr_multiplier", cfg.atr_multiplier),
            ("volume_sma_period", cfg.volume_sma_period),
            ("volume_threshold", cfg.volume_threshold),
            ("use_rsi_filter", True),
            ("use_volume_filter", True),
            ("use_trailing_stop", cfg.use_trailing_stop),
        )

    def __init__(self):
        self.sma = bt.indicators.SMA(self.data.close, period=self.params.sma_period)
        self.crossover = bt.indicators.CrossOver(self.data.close, self.sma)
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)
        self.atr = bt.indicators.ATR(self.data, period=self.params.atr_period)
        self.volume_sma = bt.indicators.SMA(
            self.data.volume, period=self.params.volume_sma_period
        )
        self.highest_price_achieved = None
        self.trigger_floor = None
        self.order = None
        self._min_period = max(
            self.params.sma_period,
            self.params.rsi_period,
            self.params.atr_period,
            self.params.volume_sma_period,
        )

    def notify_order(self, order):
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
        if not self.params.use_volume_filter:
            return True
        return float(self.data.volume[0]) > float(self.volume_sma[0]) * float(
            self.params.volume_threshold
        )

    def _rsi_allows_entry(self) -> bool:
        if not self.params.use_rsi_filter:
            return True
        return self.rsi[0] >= self.params.rsi_buy_threshold

    def _rsi_triggers_exit(self) -> bool:
        """
        RSI exit criteria aligned with analytics.LiveSignalEngine._rsi_triggers_exit.

        crossdown: RSI_{t-1} >= upper_limit and RSI_t < upper_limit
        threshold: RSI_t > upper_limit
        """
        if not self.params.use_rsi_filter:
            return False

        upper = self.params.rsi_sell_threshold
        if self.params.rsi_exit_mode == "threshold":
            return float(self.rsi[0]) > upper
        return (
            float(self.rsi[-1]) >= upper and float(self.rsi[0]) < upper
        )

    def next(self):
        if len(self) < self._min_period:
            return

        if self.order:
            return

        close = self.data.close[0]
        bar_low = self.data.low[0]

        if self.position:
            if self._dynamic_atr_stop_triggered(bar_low):
                self.order = self.close()
                return

            self._update_trailing_state(close)

            if self.crossover < 0 or self._rsi_triggers_exit():
                self.order = self.close()
                return
        else:
            if (
                self.crossover > 0
                and self._rsi_allows_entry()
                and self._passes_volume_filter()
            ):
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
    config: StrategyConfig | None = None,
    initial_cash: float | None = None,
    commission_rate: float = DEFAULT_COMMISSION_RATE,
) -> bt.Cerebro:
    """
    Initialize and orchestrate a Backtrader engine for SmaCross backtests.

    Usage:
        cerebro = create_backtest_cerebro()
        cerebro.adddata(data_feed)
        cerebro.run()
    """
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(
        initial_cash if initial_cash is not None else default_capital_at_risk()
    )
    configure_backtest_broker(cerebro, commission_rate=commission_rate)

    cfg = config or load_backtest_config()
    strategy_params = dict(SmaCross.params_from_config(cfg))
    cerebro.addstrategy(SmaCross, **strategy_params)
    return cerebro
