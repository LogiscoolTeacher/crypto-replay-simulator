"""Command-line candle downloader.

Examples (run from the backend/ folder):
    python -m app.data.download --symbol BTCUSDT --interval 1h --days 365
    python -m app.data.download --symbol ETHUSDT --interval 1D --start 2017-01-01
    python -m app.data.download --all --interval 1D --start 2017-01-01
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from .. import config
from .candle_store import CandleStore


def _parse_date(value: str) -> int:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Download Binance candles into the local SQLite store.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--symbol", help="e.g. BTCUSDT")
    g.add_argument("--all", action="store_true", help="all configured coins")
    p.add_argument("--interval", required=True, choices=list(config.INTERVALS_MS))
    r = p.add_mutually_exclusive_group(required=True)
    r.add_argument("--days", type=int, help="how many days back from now")
    r.add_argument("--start", help="ISO start date, UTC")
    p.add_argument("--end", help="ISO end date, UTC (default: now)")
    p.add_argument("--db", default=str(config.DB_PATH))
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    symbols = config.SYMBOLS if args.all else [config.validate_symbol(args.symbol)]
    if args.days is not None:
        start_ms = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)
    else:
        start_ms = _parse_date(args.start)
    end_ms = _parse_date(args.end) if args.end else None

    store = CandleStore(args.db)
    for symbol in symbols:
        t0 = time.time()
        last_print = [0.0]

        def progress(candles: int, reqs: int) -> None:
            if time.time() - last_print[0] >= 1.0:
                print(f"  {symbol} {args.interval}: {candles} candles, {reqs} requests", end="\r", flush=True)
                last_print[0] = time.time()

        added = store.ensure_range(symbol, args.interval, start_ms, end_ms, progress=progress)
        print(f"{symbol} {args.interval}: +{added} candles in {time.time() - t0:.1f}s".ljust(60))

    print("\nStore contents:")
    for row in store.stats():
        first = datetime.fromtimestamp(row["first_ms"] / 1000, tz=timezone.utc).date()
        last = datetime.fromtimestamp(row["last_ms"] / 1000, tz=timezone.utc)
        print(f"  {row['symbol']:9s} {row['interval']:3s} {row['count']:>8,d} candles  {first} .. {last:%Y-%m-%d %H:%M}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
