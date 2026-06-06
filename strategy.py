import backtrader as bt


class SmaCross(bt.Strategy):
    """SMA crossover strategy with RSI momentum filter for US equity backtesting."""

    params = (
        ("sma_period", 10),
        ("rsi_period", 14),
        ("rsi_buy_threshold", 50),
        ("rsi_sell_threshold", 70),
    )

    def __init__(self):
        self.sma = bt.indicators.SMA(self.data.close, period=self.params.sma_period)
        self.crossover = bt.indicators.CrossOver(self.data.close, self.sma)
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)

    def next(self):
        if not self.position:
            if self.crossover > 0 and self.rsi[0] >= self.params.rsi_buy_threshold:
                self.buy()
        else:
            if self.crossover < 0 or self.rsi[0] > self.params.rsi_sell_threshold:
                self.sell()
