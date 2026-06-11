"""US equity watchlist routing and benchmark symbols for KIS OpenAPI."""

from __future__ import annotations

BENCHMARK_TICKER = "SPY"
BENCHMARK_SMA_PERIOD = 200

DEFAULT_WATCHLIST: list[str] = [
    "AAPL",
    "MSFT",
    "NVDA",
    "META",
    "AMZN",
    "GOOGL",
    "TSLA",
    "AMD",
    "AVGO",
    "NFLX",
    "PLTR",
    "CRWD",
    "TSM",
    "SHOP",
    "UBER",
]

# excd / ovrs_excg_cd — VTS overseas routing (NASDAQ default; NYSE for TSM)
MARKET_META: dict[str, dict[str, str]] = {
    "NVDA": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "PLTR": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "AAPL": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "META": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "AMZN": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "MSFT": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "GOOGL": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "TSLA": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "NFLX": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "AMD": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "AVGO": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "CRWD": {"excd": "NAS", "ovrs_excg_cd": "NASD"},
    "TSM": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "SHOP": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "UBER": {"excd": "NYS", "ovrs_excg_cd": "NYSE"},
    "SPY": {"excd": "AMS", "ovrs_excg_cd": "AMEX"},
}


def parse_watchlist(raw: str | None, fallback: list[str] | None = None) -> list[str]:
    seed = fallback or DEFAULT_WATCHLIST
    if not raw:
        return seed.copy()
    tickers = [token.strip().upper() for token in raw.split(",") if token.strip()]
    return tickers or seed.copy()


def validate_watchlist_routing(tickers: list[str]) -> list[str]:
    """Return tickers missing MARKET_META entries."""
    return [ticker for ticker in tickers if ticker not in MARKET_META]
