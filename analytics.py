from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from typing import Any, Literal

import pandas as pd

ExecutionMode = Literal["eod", "next_open"]
RsiExitMode = Literal["threshold", "crossdown"]


@dataclass(frozen=True)
class StrategyConfig:
    sma_period: int = 10
    rsi_period: int = 14
    atr_period: int = 14
    volume_sma_period: int = 20
    atr_multiplier: float = 2.0
    rsi_buy_threshold: float = 50.0
    rsi_upper_limit: float = 70.0
    volume_threshold: float = 1.2
    use_trailing_stop: bool = True
    rsi_exit_mode: RsiExitMode = "crossdown"
    execution_mode: ExecutionMode = "eod"

    @classmethod
    def from_env(cls, base: StrategyConfig | None = None) -> StrategyConfig:
        """Build strategy config with optional environment overrides."""
        seed = base or cls()
        return cls(
            sma_period=int(os.getenv("SMA_PERIOD", str(seed.sma_period))),
            rsi_period=int(os.getenv("RSI_PERIOD", str(seed.rsi_period))),
            atr_period=int(os.getenv("ATR_PERIOD", str(seed.atr_period))),
            volume_sma_period=int(
                os.getenv("VOLUME_SMA_PERIOD", str(seed.volume_sma_period))
            ),
            atr_multiplier=float(
                os.getenv("ATR_MULTIPLIER", str(seed.atr_multiplier))
            ),
            rsi_buy_threshold=float(
                os.getenv("RSI_BUY_THRESHOLD", str(seed.rsi_buy_threshold))
            ),
            rsi_upper_limit=float(
                os.getenv("RSI_UPPER_LIMIT", str(seed.rsi_upper_limit))
            ),
            volume_threshold=float(
                os.getenv("VOLUME_THRESHOLD", str(seed.volume_threshold))
            ),
            use_trailing_stop=os.getenv("USE_TRAILING_STOP", "true").lower()
            in {"1", "true", "yes"},
            rsi_exit_mode=os.getenv("RSI_EXIT_MODE", seed.rsi_exit_mode),  # type: ignore[arg-type]
            execution_mode=os.getenv("EXECUTION_MODE", seed.execution_mode),  # type: ignore[arg-type]
        )


@dataclass
class PositionState:
    """
    Unified runtime registry: position, trailing stop, and order lifecycle.

    Persisted fields include has_position (in_position), pending_order,
    last_processed_date, latest_bar_date, held_quantity, and session_low.
    """

    in_position: bool = False
    pending_order: bool = False
    last_processed_date: str | None = None
    latest_bar_date: str | None = None
    held_quantity: int = 0
    session_low: float | None = None
    highest_price_achieved: float | None = None
    current_atr: float | None = None
    dynamic_stop_distance: float | None = None
    trigger_floor: float | None = None

    @property
    def has_position(self) -> bool:
        return self.in_position

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> PositionState:
        if not payload:
            return cls()

        valid = {field.name for field in fields(cls)}
        filtered = {key: payload[key] for key in payload if key in valid}
        if "held_quantity" in filtered:
            filtered["held_quantity"] = int(filtered["held_quantity"] or 0)
        if "in_position" in filtered:
            filtered["in_position"] = bool(filtered["in_position"])
        if "pending_order" in filtered:
            filtered["pending_order"] = bool(filtered["pending_order"])
        return cls(**filtered)


@dataclass
class BarSnapshot:
    date: Any
    open: float
    high: float
    low: float
    close: float
    volume: float
    sma: float
    rsi: float
    atr: float
    volume_sma: float

    @classmethod
    def from_row(cls, row: pd.Series) -> BarSnapshot:
        return cls(
            date=pd.Timestamp(row["Date"]).date(),
            open=float(row["Open"]),
            high=float(row["High"]),
            low=float(row["Low"]),
            close=float(row["Close"]),
            volume=float(row["Volume"]),
            sma=float(row["SMA"]),
            rsi=float(row["RSI"]),
            atr=float(row["ATR"]),
            volume_sma=float(row["Volume_SMA"]),
        )

    def to_metrics_dict(self) -> dict[str, float | Any]:
        return {
            "date": self.date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "sma": self.sma,
            "rsi": self.rsi,
            "atr": self.atr,
            "volume_sma": self.volume_sma,
        }


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _format_bar_date(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _wilder_ewm(series: pd.Series, period: int) -> pd.Series:
    alpha = 1.0 / period
    return series.ewm(alpha=alpha, adjust=False).mean()


def calculate_sma(df: pd.DataFrame, period: int, column: str = "Close") -> pd.Series:
    return df[column].rolling(window=period).mean()


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = _wilder_ewm(gain, period)
    avg_loss = _wilder_ewm(loss, period)

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


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
    return _wilder_ewm(true_range, period)


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
    enriched["Volume_SMA"] = calculate_sma(
        enriched, volume_sma_period, column="Volume"
    )

    if enriched.isna().any().any():
        lookback = max(sma_period, rsi_period, atr_period, volume_sma_period)
        enriched = enriched.iloc[lookback:].dropna().reset_index(drop=True)

    return enriched


def calculate_position_size(
    capital_at_risk: float,
    risk_per_trade: float,
    entry_price: float,
    stop_distance: float,
) -> int:
    """
    Compute share count from fixed fractional risk sizing.

    Shares = floor((capital * risk_pct) / stop_distance).
    """
    if entry_price <= 0 or stop_distance <= 0 or risk_per_trade <= 0:
        return 0

    dollar_risk = capital_at_risk * risk_per_trade
    shares = int(dollar_risk / stop_distance)
    return max(shares, 0)


def is_us_equity_session(now: datetime | None = None) -> bool:
    """Weekday gate for US equity crossover evaluation (Mon-Fri)."""
    current = now or datetime.now()
    return current.weekday() < 5


def should_allow_crossover_signals(
    current_bar_date: str,
    last_processed_date: str | None,
    market_open: bool,
) -> bool:
    """
    Crossover BUY/SELL is allowed only on a new daily bar during market weekdays.
    """
    if not market_open:
        return False
    if last_processed_date is None:
        return True
    return current_bar_date != last_processed_date


class LiveSignalEngine:
    """
    Unified live signal engine with shared bar-level decision logic.

    Execution assumption: EOD signal generation on completed daily bars.
    Intraday cycles may re-evaluate Priority-1 DYNAMIC_ATR_SELL using the
    session low sequence (Low_t <= floor) without re-firing crossover entries.
    """

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or StrategyConfig()

    def enrich(self, df: pd.DataFrame) -> pd.DataFrame:
        cfg = self.config
        enriched = enrich_with_indicators(
            df,
            cfg.sma_period,
            cfg.rsi_period,
            cfg.atr_period,
            cfg.volume_sma_period,
        )
        if len(enriched) < 2:
            raise ValueError("Insufficient data length for signal evaluation.")
        return enriched

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute SMA, RSI, ATR, and Volume SMA using Wilder smoothing where applicable."""
        return self.enrich(df)

    def load_state(self, payload: dict[str, Any] | None) -> PositionState:
        return PositionState.from_dict(payload)

    def dump_state(self, state: PositionState) -> dict[str, Any]:
        return state.to_dict()

    def _passes_volume_filter(self, volume: float, volume_sma: float) -> bool:
        threshold = self.config.volume_threshold
        return volume > volume_sma * threshold

    def _golden_cross(
        self, close: float, sma: float, prev_close: float, prev_sma: float
    ) -> bool:
        return close > sma and prev_close <= prev_sma

    def _death_cross(
        self, close: float, sma: float, prev_close: float, prev_sma: float
    ) -> bool:
        return close < sma and prev_close >= prev_sma

    def _rsi_allows_entry(self, rsi: float) -> bool:
        return rsi >= self.config.rsi_buy_threshold

    def _rsi_triggers_exit(self, rsi: float, prev_rsi: float) -> bool:
        upper = self.config.rsi_upper_limit
        if self.config.rsi_exit_mode == "threshold":
            return rsi > upper
        return prev_rsi >= upper and rsi < upper

    def _update_trailing_state(
        self, state: PositionState, close: float, atr: float
    ) -> None:
        if not state.in_position:
            return

        if (
            state.highest_price_achieved is None
            or close > state.highest_price_achieved
        ):
            state.highest_price_achieved = close

        state.current_atr = atr
        state.dynamic_stop_distance = atr * self.config.atr_multiplier
        state.trigger_floor = state.highest_price_achieved - state.dynamic_stop_distance

    def _dynamic_atr_stop_triggered(
        self, state: PositionState, bar_low: float
    ) -> bool:
        if not self.config.use_trailing_stop or not state.in_position:
            return False
        if state.trigger_floor is None:
            return False
        return bar_low <= state.trigger_floor

    def _build_dynamic_atr_payload(
        self,
        state: PositionState,
        bar: BarSnapshot,
        session_low: float,
    ) -> dict[str, float]:
        return {
            "captured_peak": float(state.highest_price_achieved or bar.close),
            "current_atr": float(state.current_atr or bar.atr),
            "dynamic_stop_distance": float(state.dynamic_stop_distance or 0.0),
            "trigger_floor": float(state.trigger_floor or 0.0),
            "session_low": float(session_low),
            "current_execution_price": bar.close,
        }

    def evaluate_intraday_atr_stop(
        self,
        state: PositionState,
        bar: BarSnapshot,
        session_low: float,
        mutate_state: bool = False,
    ) -> dict[str, Any] | None:
        """
        Priority-1 intraday scan: DYNAMIC_ATR_SELL when Low_t <= trigger floor.

        Uses the running session_low for the active daily bar, not only the
        static EOD low stored in the historical dataframe row.
        """
        if not state.in_position or state.pending_order:
            return None

        working = PositionState.from_dict(state.to_dict())
        self._update_trailing_state(working, bar.close, bar.atr)
        dynamic_atr_stop = self._build_dynamic_atr_payload(
            working, bar, session_low
        )

        if not self._dynamic_atr_stop_triggered(working, session_low):
            if mutate_state:
                state.highest_price_achieved = working.highest_price_achieved
                state.current_atr = working.current_atr
                state.dynamic_stop_distance = working.dynamic_stop_distance
                state.trigger_floor = working.trigger_floor
            return None

        if mutate_state:
            state.in_position = False
            state.held_quantity = 0
            state.highest_price_achieved = None
            state.current_atr = None
            state.dynamic_stop_distance = None
            state.trigger_floor = None
            state.session_low = None

        return {
            "signal": "DYNAMIC_ATR_SELL",
            "dynamic_atr_stop": dynamic_atr_stop,
            "liquidity_ok": True,
        }

    def evaluate_bar(
        self,
        state: PositionState,
        bar: BarSnapshot,
        prev_bar: BarSnapshot,
        mutate_state: bool = True,
        allow_crossover: bool = True,
        session_low: float | None = None,
    ) -> dict[str, Any]:
        """
        Evaluate one bar using unified priority:
        DYNAMIC_ATR_SELL -> SELL -> BUY -> HOLD.

        When allow_crossover is False, only the intraday ATR stop path runs.
        """
        liquidity_ok = self._passes_volume_filter(bar.volume, bar.volume_sma)
        effective_low = session_low if session_low is not None else bar.low

        working_state = (
            PositionState.from_dict(state.to_dict()) if not mutate_state else state
        )
        dynamic_atr_stop: dict[str, float] | None = None

        if working_state.in_position:
            self._update_trailing_state(working_state, bar.close, bar.atr)
            dynamic_atr_stop = self._build_dynamic_atr_payload(
                working_state, bar, effective_low
            )

            if self._dynamic_atr_stop_triggered(working_state, effective_low):
                if mutate_state:
                    state.in_position = False
                    state.held_quantity = 0
                    state.highest_price_achieved = None
                    state.current_atr = None
                    state.dynamic_stop_distance = None
                    state.trigger_floor = None
                    state.session_low = None
                return {
                    "signal": "DYNAMIC_ATR_SELL",
                    "dynamic_atr_stop": dynamic_atr_stop,
                    "liquidity_ok": liquidity_ok,
                    "state": working_state.to_dict(),
                }

        if not allow_crossover:
            return {
                "signal": "HOLD",
                "dynamic_atr_stop": dynamic_atr_stop,
                "liquidity_ok": liquidity_ok,
                "crossover_suppressed": True,
                "state": working_state.to_dict(),
            }

        cross_above = self._golden_cross(
            bar.close, bar.sma, prev_bar.close, prev_bar.sma
        )
        cross_below = self._death_cross(
            bar.close, bar.sma, prev_bar.close, prev_bar.sma
        )

        if working_state.in_position:
            if cross_below or self._rsi_triggers_exit(bar.rsi, prev_bar.rsi):
                if mutate_state:
                    state.in_position = False
                    state.held_quantity = 0
                    state.highest_price_achieved = None
                    state.current_atr = None
                    state.dynamic_stop_distance = None
                    state.trigger_floor = None
                    state.session_low = None
                return {
                    "signal": "SELL",
                    "dynamic_atr_stop": dynamic_atr_stop,
                    "liquidity_ok": liquidity_ok,
                    "state": working_state.to_dict(),
                }

        if not working_state.in_position:
            if cross_above and self._rsi_allows_entry(bar.rsi):
                if liquidity_ok:
                    if mutate_state:
                        state.in_position = True
                        state.highest_price_achieved = bar.close
                        self._update_trailing_state(state, bar.close, bar.atr)
                    return {
                        "signal": "BUY",
                        "dynamic_atr_stop": None,
                        "liquidity_ok": True,
                        "state": (
                            state.to_dict()
                            if mutate_state
                            else working_state.to_dict()
                        ),
                    }
                return {
                    "signal": "HOLD",
                    "dynamic_atr_stop": None,
                    "liquidity_ok": False,
                    "liquidity_blocked": True,
                    "state": working_state.to_dict(),
                }

        return {
            "signal": "HOLD",
            "dynamic_atr_stop": dynamic_atr_stop,
            "liquidity_ok": liquidity_ok,
            "state": working_state.to_dict(),
        }

    def replay_state(
        self,
        enriched: pd.DataFrame,
        end_index: int | None = None,
        initial_state: dict[str, Any] | None = None,
    ) -> PositionState:
        """Replay history to derive O(1)-ready state without full recompute at runtime."""
        if len(enriched) < 2:
            raise ValueError("Insufficient data length for signal evaluation.")

        state = self.load_state(initial_state)
        last_index = len(enriched) - 1 if end_index is None else end_index

        for i in range(1, last_index + 1):
            bar = BarSnapshot.from_row(enriched.iloc[i])
            prev_bar = BarSnapshot.from_row(enriched.iloc[i - 1])
            self.evaluate_bar(
                state,
                bar,
                prev_bar,
                mutate_state=True,
                allow_crossover=True,
            )

        return state

    def update_session_low(
        self, runtime: PositionState, bar: BarSnapshot
    ) -> float:
        """Track the minimum low observed for the active daily bar."""
        bar_date = _format_bar_date(bar.date)
        if runtime.latest_bar_date != bar_date:
            runtime.latest_bar_date = bar_date
            runtime.session_low = bar.low
        elif runtime.session_low is None:
            runtime.session_low = bar.low
        else:
            runtime.session_low = min(runtime.session_low, bar.low)
        return float(runtime.session_low)

    def evaluate_trading_cycle(
        self,
        df: pd.DataFrame,
        runtime_state: dict[str, Any] | None,
        capital_at_risk: float,
        risk_per_trade: float,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Evaluate one live monitoring cycle with bar-trailing and session gates.

        Returns signal_result, metrics, sizing, and the non-mutated replay snapshot.
        """
        enriched = self.enrich(df)
        replay_end = len(enriched) - 2

        if replay_end < 1:
            raise ValueError("Insufficient data length for signal evaluation.")

        runtime = self.load_state(runtime_state)
        position_state = self.replay_state(
            enriched,
            end_index=replay_end,
            initial_state={
                key: runtime_state.get(key)
                for key in PositionState.from_dict(runtime_state).to_dict()
                if key
                not in {
                    "pending_order",
                    "last_processed_date",
                    "latest_bar_date",
                    "held_quantity",
                    "session_low",
                }
            }
            if runtime_state
            else None,
        )

        runtime.in_position = position_state.in_position
        runtime.highest_price_achieved = position_state.highest_price_achieved
        runtime.current_atr = position_state.current_atr
        runtime.dynamic_stop_distance = position_state.dynamic_stop_distance
        runtime.trigger_floor = position_state.trigger_floor
        if runtime.in_position and runtime.held_quantity <= 0:
            runtime.held_quantity = max(
                int(runtime_state.get("held_quantity", 0) if runtime_state else 0),
                0,
            )

        today = BarSnapshot.from_row(enriched.iloc[-1])
        yesterday = BarSnapshot.from_row(enriched.iloc[-2])
        current_bar_date = _format_bar_date(today.date)
        session_low = self.update_session_low(runtime, today)

        market_open = is_us_equity_session(now)
        allow_crossover = should_allow_crossover_signals(
            current_bar_date,
            runtime.last_processed_date,
            market_open,
        )

        eval_state = PositionState.from_dict(position_state.to_dict())
        eval_state.pending_order = runtime.pending_order
        eval_state.last_processed_date = runtime.last_processed_date
        eval_state.latest_bar_date = runtime.latest_bar_date
        eval_state.held_quantity = runtime.held_quantity
        eval_state.session_low = runtime.session_low

        signal_result: dict[str, Any]
        if runtime.pending_order:
            signal_result = {
                "signal": "HOLD",
                "liquidity_ok": self._passes_volume_filter(
                    today.volume, today.volume_sma
                ),
                "pending_order_locked": True,
            }
        else:
            signal_result = self.evaluate_bar(
                eval_state,
                today,
                yesterday,
                mutate_state=False,
                allow_crossover=allow_crossover,
                session_low=session_low,
            )

        stop_distance = today.atr * self.config.atr_multiplier
        position_size = calculate_position_size(
            capital_at_risk=capital_at_risk,
            risk_per_trade=risk_per_trade,
            entry_price=today.close,
            stop_distance=stop_distance,
        )

        return {
            "metrics": {
                "today": today.to_metrics_dict(),
                "yesterday": yesterday.to_metrics_dict(),
            },
            "runtime_state": runtime.to_dict(),
            "position_state": position_state.to_dict(),
            "signal_result": signal_result,
            "position_size": position_size,
            "current_bar_date": current_bar_date,
            "session_low": session_low,
            "allow_crossover": allow_crossover,
            "market_open": market_open,
            "state_snapshot": self.dump_state(position_state),
        }

    def apply_post_order_transition(
        self,
        runtime: PositionState,
        signal: str,
        filled_quantity: int,
        current_bar_date: str,
        allow_crossover: bool,
    ) -> None:
        """Mutate runtime registry after a confirmed KIS order (rt_cd == 0)."""
        if signal == "BUY":
            runtime.in_position = True
            runtime.held_quantity = filled_quantity
            runtime.pending_order = False
            if allow_crossover:
                runtime.last_processed_date = current_bar_date
            return

        if signal in {"SELL", "DYNAMIC_ATR_SELL"}:
            runtime.in_position = False
            runtime.held_quantity = 0
            runtime.highest_price_achieved = None
            runtime.current_atr = None
            runtime.dynamic_stop_distance = None
            runtime.trigger_floor = None
            runtime.session_low = None
            runtime.pending_order = False
            if allow_crossover and signal == "SELL":
                runtime.last_processed_date = current_bar_date
            elif signal == "DYNAMIC_ATR_SELL":
                runtime.last_processed_date = current_bar_date
            return

        runtime.pending_order = False

    def mark_crossover_processed(
        self,
        runtime: PositionState,
        current_bar_date: str,
        allow_crossover: bool,
        signal: str,
    ) -> None:
        """Record that crossover logic ran for this daily bar without duplication."""
        if not allow_crossover:
            return
        if signal in {"BUY", "SELL", "HOLD"}:
            runtime.last_processed_date = current_bar_date

    def evaluate_session(
        self,
        df: pd.DataFrame,
        external_state: dict[str, Any] | None = None,
        end_index: int | None = None,
        capital_at_risk: float = 10_000.0,
        risk_per_trade: float = 0.01,
    ) -> dict[str, Any]:
        """Backward-compatible session wrapper used by tests and legacy callers."""
        cycle = self.evaluate_trading_cycle(
            df,
            runtime_state=external_state,
            capital_at_risk=capital_at_risk,
            risk_per_trade=risk_per_trade,
        )
        return {
            "metrics": cycle["metrics"],
            "position_state": cycle["position_state"],
            "signal_result": cycle["signal_result"],
            "position_size": cycle["position_size"],
            "state_snapshot": cycle["state_snapshot"],
        }


def passes_volume_filter(
    metrics: dict,
    volume_threshold: float = 1.2,
) -> bool:
    today = metrics["today"]
    return float(today["volume"]) > float(today["volume_sma"]) * volume_threshold


def extract_latest_metrics(
    df: pd.DataFrame,
    sma_period: int,
    rsi_period: int,
    atr_period: int,
    volume_sma_period: int,
) -> dict:
    engine = LiveSignalEngine(
        StrategyConfig(
            sma_period=sma_period,
            rsi_period=rsi_period,
            atr_period=atr_period,
            volume_sma_period=volume_sma_period,
        )
    )
    enriched = engine.enrich(df)
    today = BarSnapshot.from_row(enriched.iloc[-1])
    yesterday = BarSnapshot.from_row(enriched.iloc[-2])
    return {
        "today": today.to_metrics_dict(),
        "yesterday": yesterday.to_metrics_dict(),
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
    volume_threshold: float = 1.2,
    external_state: dict[str, Any] | None = None,
) -> dict:
    engine = LiveSignalEngine(
        StrategyConfig(
            sma_period=sma_period,
            rsi_period=rsi_period,
            atr_period=atr_period,
            volume_sma_period=volume_sma_period,
            atr_multiplier=atr_multiplier,
            rsi_buy_threshold=rsi_buy_threshold,
            rsi_upper_limit=rsi_sell_threshold,
            volume_threshold=volume_threshold,
        )
    )
    enriched = engine.enrich(df)
    state = engine.replay_state(
        enriched,
        end_index=end_index,
        initial_state=external_state,
    )
    return state.to_dict()


def evaluate_live_signal(
    metrics: dict,
    position_state: dict,
    atr_multiplier: float = 2.0,
    rsi_buy_threshold: float = 50,
    rsi_sell_threshold: float = 70,
    volume_threshold: float = 1.2,
    rsi_exit_mode: RsiExitMode = "crossdown",
) -> dict:
    engine = LiveSignalEngine(
        StrategyConfig(
            atr_multiplier=atr_multiplier,
            rsi_buy_threshold=rsi_buy_threshold,
            rsi_upper_limit=rsi_sell_threshold,
            volume_threshold=volume_threshold,
            rsi_exit_mode=rsi_exit_mode,
        )
    )

    today = BarSnapshot(
        date=metrics["today"]["date"],
        open=float(metrics["today"].get("open", metrics["today"]["close"])),
        high=float(metrics["today"].get("high", metrics["today"]["close"])),
        low=float(metrics["today"].get("low", metrics["today"]["close"])),
        close=float(metrics["today"]["close"]),
        volume=float(metrics["today"]["volume"]),
        sma=float(metrics["today"]["sma"]),
        rsi=float(metrics["today"]["rsi"]),
        atr=float(metrics["today"]["atr"]),
        volume_sma=float(metrics["today"]["volume_sma"]),
    )
    yesterday = BarSnapshot(
        date=metrics["yesterday"]["date"],
        open=float(metrics["yesterday"].get("open", metrics["yesterday"]["close"])),
        high=float(metrics["yesterday"].get("high", metrics["yesterday"]["close"])),
        low=float(metrics["yesterday"].get("low", metrics["yesterday"]["close"])),
        close=float(metrics["yesterday"]["close"]),
        volume=float(metrics["yesterday"]["volume"]),
        sma=float(metrics["yesterday"]["sma"]),
        rsi=float(metrics["yesterday"]["rsi"]),
        atr=float(metrics["yesterday"]["atr"]),
        volume_sma=float(metrics["yesterday"]["volume_sma"]),
    )

    state = engine.load_state(position_state)
    result = engine.evaluate_bar(
        state,
        today,
        yesterday,
        mutate_state=False,
        allow_crossover=True,
    )
    return {k: v for k, v in result.items() if k != "state"}
