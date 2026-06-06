import pandas as pd


def calculate_sma(df: pd.DataFrame, period: int, column: str = "Close") -> pd.Series:
    return df[column].rolling(window=period).mean()


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr_components = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    )
    true_range = tr_components.max(axis=1)
    return true_range.rolling(window=period).mean()


def enrich_with_indicators(
    df: pd.DataFrame,
    sma_period: int,
    rsi_period: int,
    atr_period: int,
    volume_sma_period: int,
) -> pd.DataFrame:
    enriched = df.copy()
    enriched["SMA"] = calculate_sma(enriched, sma_period)
    enriched["RSI"] = calculate_rsi(enriched, rsi_period)
    enriched["ATR"] = calculate_atr(enriched, atr_period)
    enriched["Volume_SMA"] = calculate_sma(enriched, volume_sma_period, column="Volume")
    return enriched


def passes_volume_filter(metrics: dict) -> bool:
    """Return True when latest volume meets the 20-day liquidity threshold."""
    return metrics["today"]["volume"] >= metrics["today"]["volume_sma"]


def extract_latest_metrics(
    df: pd.DataFrame,
    sma_period: int,
    rsi_period: int,
    atr_period: int,
    volume_sma_period: int,
) -> dict:
    enriched = enrich_with_indicators(
        df, sma_period, rsi_period, atr_period, volume_sma_period
    )
    today = enriched.iloc[-1]
    yesterday = enriched.iloc[-2]

    return {
        "today": {
            "date": pd.Timestamp(today["Date"]).date(),
            "close": float(today["Close"]),
            "volume": float(today["Volume"]),
            "sma": float(today["SMA"]),
            "rsi": float(today["RSI"]),
            "atr": float(today["ATR"]),
            "volume_sma": float(today["Volume_SMA"]),
        },
        "yesterday": {
            "date": pd.Timestamp(yesterday["Date"]).date(),
            "close": float(yesterday["Close"]),
            "volume": float(yesterday["Volume"]),
            "sma": float(yesterday["SMA"]),
            "rsi": float(yesterday["RSI"]),
            "atr": float(yesterday["ATR"]),
            "volume_sma": float(yesterday["Volume_SMA"]),
        },
    }


def derive_position_state(
    df: pd.DataFrame,
    sma_period: int,
    rsi_period: int,
    atr_period: int,
    volume_sma_period: int,
    atr_multiplier: float,
    rsi_buy_threshold: float,
    rsi_sell_threshold: float,
    end_index: int | None = None,
) -> dict:
    """Replay strategy rules to derive position and dynamic ATR stop state."""
    enriched = enrich_with_indicators(
        df, sma_period, rsi_period, atr_period, volume_sma_period
    )
    last_index = len(enriched) - 1 if end_index is None else end_index

    in_position = False
    highest_price_achieved = None
    current_atr = None
    dynamic_stop_distance = None
    trigger_floor = None

    for i in range(1, last_index + 1):
        row = enriched.iloc[i]
        prev = enriched.iloc[i - 1]
        close = float(row["Close"])
        volume = float(row["Volume"])
        volume_sma = float(row["Volume_SMA"])
        sma = float(row["SMA"])
        rsi = float(row["RSI"])
        atr = float(row["ATR"])
        prev_close = float(prev["Close"])
        prev_sma = float(prev["SMA"])

        cross_above = close > sma and prev_close <= prev_sma
        cross_below = close < sma and prev_close >= prev_sma
        liquidity_ok = volume >= volume_sma

        if in_position:
            if highest_price_achieved is None or close > highest_price_achieved:
                highest_price_achieved = close

            current_atr = atr
            dynamic_stop_distance = atr * atr_multiplier
            trigger_floor = highest_price_achieved - dynamic_stop_distance

            if close <= trigger_floor:
                in_position = False
                highest_price_achieved = None
                current_atr = None
                dynamic_stop_distance = None
                trigger_floor = None
                continue

            if cross_below or rsi > rsi_sell_threshold:
                in_position = False
                highest_price_achieved = None
                current_atr = None
                dynamic_stop_distance = None
                trigger_floor = None
        elif cross_above and rsi >= rsi_buy_threshold and liquidity_ok:
            in_position = True
            highest_price_achieved = close
            current_atr = atr
            dynamic_stop_distance = atr * atr_multiplier
            trigger_floor = highest_price_achieved - dynamic_stop_distance

    return {
        "in_position": in_position,
        "highest_price_achieved": highest_price_achieved,
        "current_atr": current_atr,
        "dynamic_stop_distance": dynamic_stop_distance,
        "trigger_floor": trigger_floor,
    }


def evaluate_live_signal(
    metrics: dict,
    position_state: dict,
    atr_multiplier: float = 2.0,
    rsi_buy_threshold: float = 50,
    rsi_sell_threshold: float = 70,
) -> dict:
    """Evaluate live signal: DYNAMIC_ATR_SELL -> SELL -> BUY -> HOLD."""
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
    liquidity_ok = passes_volume_filter(metrics)

    dynamic_atr_stop = None

    if position_state["in_position"]:
        highest = position_state["highest_price_achieved"]
        if highest is None:
            highest = today["close"]
        elif today["close"] > highest:
            highest = today["close"]

        current_atr = today["atr"]
        dynamic_stop_distance = current_atr * atr_multiplier
        trigger_floor = highest - dynamic_stop_distance

        dynamic_atr_stop = {
            "captured_peak": highest,
            "current_atr": current_atr,
            "dynamic_stop_distance": dynamic_stop_distance,
            "trigger_floor": trigger_floor,
            "current_execution_price": today["close"],
        }

        if today["close"] <= trigger_floor:
            return {
                "signal": "DYNAMIC_ATR_SELL",
                "dynamic_atr_stop": dynamic_atr_stop,
                "liquidity_ok": liquidity_ok,
            }

    if cross_below or today["rsi"] > rsi_sell_threshold:
        return {
            "signal": "SELL",
            "dynamic_atr_stop": dynamic_atr_stop,
            "liquidity_ok": liquidity_ok,
        }

    if cross_above and today["rsi"] >= rsi_buy_threshold:
        if liquidity_ok:
            return {
                "signal": "BUY",
                "dynamic_atr_stop": dynamic_atr_stop,
                "liquidity_ok": True,
            }
        return {
            "signal": "HOLD",
            "dynamic_atr_stop": dynamic_atr_stop,
            "liquidity_ok": False,
            "liquidity_blocked": True,
        }

    return {
        "signal": "HOLD",
        "dynamic_atr_stop": dynamic_atr_stop,
        "liquidity_ok": liquidity_ok,
    }
