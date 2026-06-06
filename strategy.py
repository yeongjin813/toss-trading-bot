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
        ("enable_dynamic_atr_stop", True),
        ("enable_volume_filter", True),
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

    def _reset_trailing_state(self) -> None:
        self.highest_price_achieved = None

    def _update_trailing_state(self, close: float) -> None:
        if self.highest_price_achieved is None or close > self.highest_price_achieved:
            self.highest_price_achieved = close

    def _dynamic_stop_distance(self) -> float:
        return float(self.atr[0]) * self.params.atr_multiplier

    def _trigger_floor_price(self) -> float:
        return self.highest_price_achieved - self._dynamic_stop_distance()

    def _dynamic_atr_stop_triggered(self, close: float) -> bool:
        if not self.params.enable_dynamic_atr_stop or self.highest_price_achieved is None:
            return False
        return close <= self._trigger_floor_price()

    def _passes_volume_filter(self) -> bool:
        if not self.params.enable_volume_filter:
            return True
        return float(self.data.volume[0]) >= float(self.volume_sma[0])

    def next(self):
        close = self.data.close[0]

        if self.position:
            self._update_trailing_state(close)

            if self._dynamic_atr_stop_triggered(close):
                self.sell()
                self._reset_trailing_state()
                return

            if self.crossover < 0 or self.rsi[0] > self.params.rsi_sell_threshold:
                self.sell()
                self._reset_trailing_state()
        else:
            self._reset_trailing_state()
            if (
                self.crossover > 0
                and self.rsi[0] >= self.params.rsi_buy_threshold
                and self._passes_volume_filter()
            ):
                self.buy()
                self.highest_price_achieved = close
