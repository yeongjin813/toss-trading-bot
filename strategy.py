import backtrader as bt


class SmaCross(bt.Strategy):
    """SMA crossover strategy with RSI filter, volume gate, and dynamic ATR trailing stop."""

    params = (
        ("sma_period", 10),
        ("rsi_period", 14),
        ("rsi_buy_threshold", 50),
        ("rsi_sell_threshold", 70),
        ("atr_period", 14),
        ("atr_multiplier", 2.0),
        ("volume_sma_period", 20),
        ("use_rsi_filter", True),
        ("use_volume_filter", True),
        ("use_trailing_stop", True),
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
                elif order.issell() and not self.position:
                    self._reset_trailing_state()

            self.order = None

    def _reset_trailing_state(self) -> None:
        self.highest_price_achieved = None

    def _update_trailing_state(self, close: float) -> None:
        if self.highest_price_achieved is None or close > self.highest_price_achieved:
            self.highest_price_achieved = close

    def _dynamic_stop_distance(self) -> float:
        return float(self.atr[0]) * self.params.atr_multiplier

    def _trigger_floor_price(self) -> float:
        return self.highest_price_achieved - self._dynamic_stop_distance()

    def _dynamic_atr_stop_triggered(self, bar_low: float) -> bool:
        if not self.params.use_trailing_stop or self.highest_price_achieved is None:
            return False
        return bar_low <= self._trigger_floor_price()

    def _passes_volume_filter(self) -> bool:
        if not self.params.use_volume_filter:
            return True
        return float(self.data.volume[0]) >= float(self.volume_sma[0])

    def _rsi_allows_entry(self) -> bool:
        if not self.params.use_rsi_filter:
            return True
        return self.rsi[0] >= self.params.rsi_buy_threshold

    def _rsi_triggers_exit(self) -> bool:
        if not self.params.use_rsi_filter:
            return False
        return self.rsi[0] > self.params.rsi_sell_threshold

    def next(self):
        if len(self) < self._min_period:
            return

        if self.order:
            return

        close = self.data.close[0]
        bar_low = self.data.low[0]

        if self.position:
            self._update_trailing_state(close)

            if self._dynamic_atr_stop_triggered(bar_low):
                self.order = self.close()
                return

            if self.crossover < 0 or self._rsi_triggers_exit():
                self.order = self.close()
        else:
            if (
                self.crossover > 0
                and self._rsi_allows_entry()
                and self._passes_volume_filter()
            ):
                self.order = self.buy()
