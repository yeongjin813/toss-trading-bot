import os
import tempfile

import backtrader as bt
import pandas as pd

from strategy import SmaCross

DATA_PATH = "./data/aapl_daily.csv"
INITIAL_CASH = 10000.0
SMA_PERIODS = range(10, 55, 5)


class FinalPortfolioValue(bt.Analyzer):
    def stop(self):
        self.rets = {"final_value": self.strategy.broker.getvalue()}


def load_csv_data(path: str) -> bt.feeds.GenericCSVData:
    df = pd.read_csv(
        path,
        skiprows=3,
        names=["Date", "Close", "High", "Low", "Open", "Volume"],
    )
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]

    fd, bt_path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    df.to_csv(bt_path, index=False)

    return bt.feeds.GenericCSVData(
        dataname=bt_path,
        dtformat="%Y-%m-%d",
        datetime=0,
        open=1,
        high=2,
        low=3,
        close=4,
        volume=5,
        openinterest=-1,
    )


def main() -> None:
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(INITIAL_CASH)

    data = load_csv_data(DATA_PATH)
    cerebro.adddata(data)

    cerebro.optstrategy(SmaCross, sma_period=SMA_PERIODS)
    cerebro.addanalyzer(FinalPortfolioValue, _name="final_value")

    opt_results = cerebro.run(maxcpus=1)

    portfolio_results = []
    for run in opt_results:
        for opt_return in run:
            analysis = opt_return.analyzers.final_value.get_analysis()
            portfolio_results.append(
                {
                    "sma_period": opt_return.params.sma_period,
                    "final_value": analysis["final_value"],
                }
            )

    portfolio_results.sort(key=lambda item: item["final_value"], reverse=True)

    print("Parameter Optimization Results")
    print("=" * 50)
    print(f"{'SMA Period':<12} {'Final Portfolio Value':>22}")
    print("-" * 50)
    for result in sorted(portfolio_results, key=lambda item: item["sma_period"]):
        print(
            f"{result['sma_period']:<12} "
            f"${result['final_value']:>20,.2f}"
        )

    best = portfolio_results[0]
    print("=" * 50)
    print(
        f"Best Parameter: SMA Period = {best['sma_period']} | "
        f"Final Portfolio Value: ${best['final_value']:,.2f}"
    )


if __name__ == "__main__":
    main()
