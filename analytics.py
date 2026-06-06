import pandas as pd


def calculate_sma(df: pd.DataFrame, period: int) -> pd.Series:
    return df["Close"].rolling(window=period).mean()


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def enrich_with_indicators(
    df: pd.DataFrame,
    sma_period: int,
    rsi_period: int,
) -> pd.DataFrame:
    enriched = df.copy()
    enriched["SMA"] = calculate_sma(enriched, sma_period)
    enriched["RSI"] = calculate_rsi(enriched, rsi_period)
    return enriched


def extract_latest_metrics(
    df: pd.DataFrame,
    sma_period: int,
    rsi_period: int,
) -> dict:
    enriched = enrich_with_indicators(df, sma_period, rsi_period)
    today = enriched.iloc[-1]
    yesterday = enriched.iloc[-2]

    return {
        "today": {
            "date": pd.Timestamp(today["Date"]).date(),
            "close": float(today["Close"]),
            "sma": float(today["SMA"]),
            "rsi": float(today["RSI"]),
        },
        "yesterday": {
            "date": pd.Timestamp(yesterday["Date"]).date(),
            "close": float(yesterday["Close"]),
            "sma": float(yesterday["SMA"]),
            "rsi": float(yesterday["RSI"]),
        },
    }


def evaluate_live_signal(
    metrics: dict,
    rsi_buy_threshold: float = 50,
    rsi_sell_threshold: float = 70,
) -> str:
    today = metrics["today"]
    yesterday = metrics["yesterday"]

    cross_above = (
        today["close"] > today["sma"]
        and yesterday["close"] <= yesterday["sma"]
    )
    cross_below = (
        today["close"] < today["sma"]
        and yesterday["close"] >= yesterday["sma"]
    )

    if cross_below or today["rsi"] > rsi_sell_threshold:
        return "SELL"
    if cross_above and today["rsi"] >= rsi_buy_threshold:
        return "BUY"
    return "HOLD"
