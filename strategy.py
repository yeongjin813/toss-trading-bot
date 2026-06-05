import backtrader as bt


class SmaCross(bt.Strategy):
    params = (
        ("sma_period", 20),
        ("rsi_period", 14),
        ("rsi_buy_threshold", 50),
        ("rsi_sell_threshold", 70),
    )

    def __init__(self):
        # Trend filter: price crossing above/below the configurable SMA period
        self.sma = bt.indicators.SMA(self.data.close, period=self.params.sma_period)
        self.crossover = bt.indicators.CrossOver(self.data.close, self.sma)

        # Momentum filter: RSI to reduce whipsaw entries and exit overbought conditions
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)

    def next(self):
        if not self.position:
            # Enter only on golden cross with sufficient bullish momentum (RSI >= 50)
            if self.crossover > 0 and self.rsi[0] >= self.params.rsi_buy_threshold:
                self.buy()
                # print(
                #     f"[BUY] {self.data.datetime.date(0)} | "
                #     f"Price: {self.data.close[0]:.2f} | RSI: {self.rsi[0]:.2f}"
                # )
        else:
            # Exit on death cross or when RSI signals overbought territory (> 70)
            if self.crossover < 0 or self.rsi[0] > self.params.rsi_sell_threshold:
                self.sell()
                # print(
                #     f"[SELL] {self.data.datetime.date(0)} | "
                #     f"Price: {self.data.close[0]:.2f} | RSI: {self.rsi[0]:.2f}"
                # )
