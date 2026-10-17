"""Central configuration for the replay trading backend.

All decisions here were agreed with the user on 2026-10-10. Change deliberately.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("REPLAY_DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = DATA_DIR / "replay.db"
FRONTEND_DIR = PROJECT_ROOT / "frontend"

QUOTE_ASSET = "USDT"

# Top 10 by market cap on 2026-10-10 (CoinGecko), stablecoins and tokenized
# assets excluded. All verified as TRADING spot pairs against USDT on Binance.
COINS: list[dict[str, str]] = [
    {"base": "BTC", "name": "Bitcoin"},
    {"base": "ETH", "name": "Ethereum"},
    {"base": "BNB", "name": "BNB"},
    {"base": "XRP", "name": "XRP"},
    {"base": "SOL", "name": "Solana"},
    {"base": "TRX", "name": "TRON"},
    {"base": "ZEC", "name": "Zcash"},
    {"base": "HYPE", "name": "Hyperliquid"},
    {"base": "DOGE", "name": "Dogecoin"},
    {"base": "LINK", "name": "Chainlink"},
]
for _c in COINS:
    _c["symbol"] = _c["base"] + QUOTE_ASSET

SYMBOLS: list[str] = [c["symbol"] for c in COINS]

# UI interval name -> length in milliseconds. Binance candles for every one of
# these open on exact multiples of the interval length in UTC.
INTERVALS_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1D": 24 * 60 * 60_000,
}

# Binance spells the daily interval in lowercase; the rest match the UI names.
BINANCE_INTERVAL: dict[str, str] = {name: ("1d" if name == "1D" else name) for name in INTERVALS_MS}

# Trading defaults (editable per session in later steps).
DEFAULT_START_BALANCE = 10_000.0
DEFAULT_FEE_RATE = 0.001  # 0.1 % per side


def interval_ms(interval: str) -> int:
    try:
        return INTERVALS_MS[interval]
    except KeyError:
        raise ValueError(f"Unknown interval {interval!r}; expected one of {list(INTERVALS_MS)}") from None


def validate_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        raise ValueError(f"Unknown symbol {symbol!r}; expected one of {SYMBOLS}")
    return symbol
