"""SQLite-backed store of OHLCV candles with download-coverage tracking.

Design notes
------------
* Candles are keyed by (symbol, interval, open_time_ms). Times are stored in
  milliseconds exactly as Binance reports them.
* A `coverage` table records which [start, end] slot ranges have already been
  requested from Binance. Real gaps in Binance history (exchange downtime,
  pre-listing periods) therefore are never re-requested.
* Only CLOSED candles are stored. A candle with open time t is closed once
  t + interval <= now. The forming candle is always dropped.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from ..config import BINANCE_INTERVAL, interval_ms, validate_symbol
from .binance_client import MAX_KLINES_PER_REQUEST, BinanceClient, Kline

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int], None]  # (candles_downloaded_so_far, requests_made)

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol     TEXT    NOT NULL,
    interval   TEXT    NOT NULL,
    open_time  INTEGER NOT NULL,
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    volume     REAL    NOT NULL,
    taker_buy  REAL,              -- taker-buy base volume; NULL for candles stored before it was kept
    PRIMARY KEY (symbol, interval, open_time)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS coverage (
    symbol    TEXT    NOT NULL,
    interval  TEXT    NOT NULL,
    start_ms  INTEGER NOT NULL,
    end_ms    INTEGER NOT NULL,
    PRIMARY KEY (symbol, interval, start_ms)
);
"""


def floor_to_slot(ts_ms: int, step_ms: int) -> int:
    return (int(ts_ms) // step_ms) * step_ms


def last_closed_open_time(now_ms: int, step_ms: int) -> int:
    """Open time of the most recent candle that has fully closed at `now_ms`."""
    return floor_to_slot(now_ms, step_ms) - step_ms


class CandleStore:
    def __init__(self, db_path: Path | str, client: BinanceClient | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client or BinanceClient()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Volume delta backfill (see fill_taker_buy_soon): the series being filled, and when a fill
        # of a series last failed, so a view never waits for it and offline it isn't retried at every step.
        self._fill_running: set[tuple[str, str]] = set()
        self._fill_failed: dict[tuple[str, str], float] = {}
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # additive migration: databases from before the volume delta lack the column
            if "taker_buy" not in {r[1] for r in conn.execute("PRAGMA table_info(candles)")}:
                conn.execute("ALTER TABLE candles ADD COLUMN taker_buy REAL")

    # ------------------------------------------------------------ connections
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success, rolls back on error and always closes."""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _series_lock(self, symbol: str, interval: str) -> threading.Lock:
        """One lock per (symbol, interval): concurrent downloads of the same series serialize."""
        with self._locks_guard:
            return self._locks.setdefault((symbol, interval), threading.Lock())

    # --------------------------------------------------------------- coverage
    def coverage(self, symbol: str, interval: str) -> list[tuple[int, int]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT start_ms, end_ms FROM coverage WHERE symbol=? AND interval=? ORDER BY start_ms",
                (symbol, interval),
            ).fetchall()
        return [(int(s), int(e)) for s, e in rows]

    def _add_coverage(self, conn: sqlite3.Connection, symbol: str, interval: str, start_ms: int, end_ms: int) -> None:
        """Insert [start_ms, end_ms] and merge with overlapping or adjacent ranges."""
        step = interval_ms(interval)
        conn.execute("BEGIN IMMEDIATE")  # read-modify-write of the coverage rows must be atomic
        rows = conn.execute(
            "SELECT start_ms, end_ms FROM coverage WHERE symbol=? AND interval=?",
            (symbol, interval),
        ).fetchall()
        ranges = sorted([(int(s), int(e)) for s, e in rows] + [(start_ms, end_ms)])
        merged: list[tuple[int, int]] = []
        for s, e in ranges:
            if merged and s <= merged[-1][1] + step:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        conn.execute("DELETE FROM coverage WHERE symbol=? AND interval=?", (symbol, interval))
        conn.executemany(
            "INSERT INTO coverage(symbol, interval, start_ms, end_ms) VALUES (?,?,?,?)",
            [(symbol, interval, s, e) for s, e in merged],
        )

    def missing_ranges(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        """Slot-aligned sub-ranges of [start_ms, end_ms] not yet covered."""
        step = interval_ms(interval)
        start = floor_to_slot(start_ms, step)
        end = floor_to_slot(end_ms, step)
        if end < start:
            return []
        gaps: list[tuple[int, int]] = []
        cursor = start
        for s, e in self.coverage(symbol, interval):
            if e < cursor:
                continue
            if s > end:
                break
            if s > cursor:
                gaps.append((cursor, s - step))
            cursor = max(cursor, e + step)
            if cursor > end:
                break
        if cursor <= end:
            gaps.append((cursor, end))
        return gaps

    # --------------------------------------------------------------- download
    def ensure_range(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int | None = None,
        progress: ProgressFn | None = None,
    ) -> int:
        """Download whatever part of [start_ms, end_ms] is missing. Returns candles added."""
        symbol = validate_symbol(symbol)
        step = interval_ms(interval)
        with self._series_lock(symbol, interval):
            # Nothing missing by the local clock: no network at all (preparing works offline). A
            # clock running ahead only shows a gap, which the server time below then settles.
            local_latest = last_closed_open_time(int(time.time() * 1000) - 2000, step)
            if not self.missing_ranges(symbol, interval, start_ms, local_latest if end_ms is None else min(int(end_ms), local_latest)):
                return 0
            now_ms = self._now_ms()
            latest = last_closed_open_time(now_ms, step)
            end_ms = latest if end_ms is None else min(int(end_ms), latest)
            gaps = self.missing_ranges(symbol, interval, start_ms, end_ms)
            if not gaps:
                return 0
            added = 0
            requests_made = 0
            for gap_start, gap_end in gaps:
                got, reqs = self._download_gap(
                    symbol, interval, gap_start, gap_end, now_ms, progress, added, requests_made
                )
                added += got
                requests_made += reqs
            return added

    def _download_gap(
        self,
        symbol: str,
        interval: str,
        gap_start: int,
        gap_end: int,
        now_ms: int,
        progress: ProgressFn | None,
        added_before: int,
        reqs_before: int,
    ) -> tuple[int, int]:
        step = interval_ms(interval)
        b_interval = BINANCE_INTERVAL[interval]
        cursor = gap_start
        added = 0
        reqs = 0
        covered_to = gap_start - step  # last slot confirmed fetched
        while cursor <= gap_end:
            rows = self.client.klines(symbol, b_interval, start_ms=cursor, end_ms=gap_end + step - 1)
            reqs += 1
            closed = [k for k in rows if k.close_time < now_ms and cursor <= k.open_time <= gap_end]
            if closed:
                self._insert(symbol, interval, closed)
                added += len(closed)
                covered_to = closed[-1].open_time
            if progress:
                progress(added_before + added, reqs_before + reqs)
            if len(rows) < MAX_KLINES_PER_REQUEST:
                # Binance had nothing more in this window: we reached gap_end, or the
                # listing date lies inside the window, or a real gap exists.
                covered_to = gap_end
                break
            cursor = rows[-1].open_time + step
        else:
            covered_to = gap_end
        if covered_to >= gap_start:
            with self._conn() as conn:
                self._add_coverage(conn, symbol, interval, gap_start, covered_to)
        return added, reqs

    def _insert(self, symbol: str, interval: str, klines: list[Kline]) -> None:
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO candles(symbol, interval, open_time, open, high, low, close, volume, taker_buy) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(symbol, interval, k.open_time, k.open, k.high, k.low, k.close, k.volume, k.taker_buy) for k in klines],
            )

    # ------------------------------------------------------- volume delta backfill
    def missing_taker_buy(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> tuple[int, int] | None:
        """(first, last) open time of the stored candles in [start_ms, end_ms] without a taker-buy
        volume (downloaded before it was kept), or None when every candle has one."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MIN(open_time), MAX(open_time) FROM candles WHERE symbol=? AND interval=? "
                "AND open_time BETWEEN ? AND ? AND taker_buy IS NULL",
                (symbol, interval, int(start_ms), int(end_ms)),
            ).fetchone()
        return None if row is None or row[0] is None else (int(row[0]), int(row[1]))

    def fill_taker_buy(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> int:
        """Download the candles of [start_ms, end_ms] again that have no taker-buy volume yet and
        store them with it (prices and volumes are the same). Returns the candles rewritten."""
        span = self.missing_taker_buy(symbol, interval, start_ms, end_ms)
        if span is None:
            return 0
        step = interval_ms(interval)
        cursor, last = span
        done = 0
        with self._series_lock(symbol, interval):
            while cursor <= last:
                rows = self.client.klines(symbol, BINANCE_INTERVAL[interval], start_ms=cursor, end_ms=last + step - 1)
                # only candles already stored (closed), so the coverage stays as it is
                keep = [k for k in rows if cursor <= k.open_time <= last and k.taker_buy is not None]
                if keep:
                    with self._conn() as conn:
                        conn.executemany(
                            "UPDATE candles SET taker_buy=? WHERE symbol=? AND interval=? AND open_time=?",
                            [(k.taker_buy, symbol, interval, k.open_time) for k in keep],
                        )
                    done += len(keep)
                if len(rows) < MAX_KLINES_PER_REQUEST:
                    break
                cursor = rows[-1].open_time + step
        return done

    def fill_taker_buy_soon(self, symbol: str, interval: str, start_ms: int, end_ms: int,
                            fill_end_ms: int | None = None, retry_s: float = 600.0) -> bool:
        """Start `fill_taker_buy` in a background thread when [start_ms, end_ms] lacks taker-buy
        volumes, so the chart never waits for the exchange (offline it only logs). The fill runs to
        `fill_end_ms` (default `end_ms`): ahead of what is checked, so a replay stepping forward
        finds its next candles filled already. After a failed fill the series waits `retry_s`
        before the next try. Returns True when a fill was started."""
        key = (symbol, interval)
        failed = self._fill_failed.get(key)
        if key in self._fill_running or (failed is not None and time.monotonic() - failed < retry_s):
            return False
        if self.missing_taker_buy(symbol, interval, start_ms, end_ms) is None:
            return False
        self._fill_running.add(key)

        def run() -> None:
            try:
                n = self.fill_taker_buy(symbol, interval, start_ms, max(end_ms, fill_end_ms or end_ms))
                log.info("Volume delta: taker-buy volume added to %d %s %s candles", n, symbol, interval)
            except Exception as exc:  # offline or rate limited: the delta stays empty for now
                self._fill_failed[key] = time.monotonic()
                log.warning("Volume delta backfill of %s %s failed: %s", symbol, interval, exc)
            finally:
                self._fill_running.discard(key)

        threading.Thread(target=run, name=f"taker-fill-{symbol}-{interval}", daemon=True).start()
        return True

    def _now_ms(self) -> int:
        try:
            return self.client.server_time_ms()
        except Exception as exc:  # fall back to the local clock with a safety margin
            log.warning("Could not read Binance server time (%s); using local clock", exc)
            return int(time.time() * 1000) - 2000

    # ------------------------------------------------------------------ reads
    def get_candles(
        self,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Candles ordered by time. Columns: time (unix SECONDS), open, high, low, close, volume,
        taker_buy (NaN where not stored yet).

        With `limit`, the most recent `limit` candles of the window are returned.
        """
        sql = "SELECT open_time, open, high, low, close, volume, taker_buy FROM candles WHERE symbol=? AND interval=?"
        params: list = [symbol, interval]
        if start_ms is not None:
            sql += " AND open_time >= ?"
            params.append(int(start_ms))
        if end_ms is not None:
            sql += " AND open_time <= ?"
            params.append(int(end_ms))
        if limit is not None:
            sql = f"SELECT * FROM ({sql} ORDER BY open_time DESC LIMIT ?) ORDER BY open_time ASC"
            params.append(int(limit))
        else:
            sql += " ORDER BY open_time ASC"
        with self._conn() as conn:
            df = pd.read_sql_query(sql, conn, params=params)
        df["time"] = (df["open_time"] // 1000).astype("int64")
        df["taker_buy"] = df["taker_buy"].astype(float)   # NULL -> NaN, also in an all-NULL window
        return df[["time", "open", "high", "low", "close", "volume", "taker_buy"]]

    def first_at_or_after(self, symbol: str, interval: str, ms: int) -> int | None:
        """Open time of the first stored candle with open_time >= ms, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MIN(open_time) FROM candles WHERE symbol=? AND interval=? AND open_time>=?",
                (symbol, interval, int(ms)),
            ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def nth_at_or_after(self, symbol: str, interval: str, ms: int, n: int) -> int | None:
        """Open time of the n-th stored candle (1-based) with open_time >= ms, or None."""
        if n < 1:
            raise ValueError("n must be >= 1")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT open_time FROM candles WHERE symbol=? AND interval=? AND open_time>=? "
                "ORDER BY open_time LIMIT 1 OFFSET ?",
                (symbol, interval, int(ms), int(n) - 1),
            ).fetchone()
        return None if row is None else int(row[0])

    def last_at_or_before(self, symbol: str, interval: str, ms: int) -> int | None:
        """Open time of the last stored candle with open_time <= ms, or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(open_time) FROM candles WHERE symbol=? AND interval=? AND open_time<=?",
                (symbol, interval, int(ms)),
            ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def peak_close(self, symbol: str, interval: str, after_ms: int, until_ms: int) -> tuple[int, float] | None:
        """(open_time, close) of the highest close with after_ms < open_time <= until_ms, or None.

        Ties go to the earliest candle.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT open_time, close FROM candles WHERE symbol=? AND interval=? AND open_time>? AND open_time<=? "
                "ORDER BY close DESC, open_time ASC LIMIT 1",
                (symbol, interval, int(after_ms), int(until_ms)),
            ).fetchone()
        return None if row is None else (int(row[0]), float(row[1]))

    def extremes_between(self, symbol: str, interval: str, from_ms: int, to_ms: int) -> dict | None:
        """Highest high and lowest low (with their open times) of the candles with
        from_ms <= open_time <= to_ms, or None when there are none. Ties go to the earliest candle."""
        with self._conn() as conn:
            hi = conn.execute(
                "SELECT open_time, high FROM candles WHERE symbol=? AND interval=? AND open_time>=? AND open_time<=? "
                "ORDER BY high DESC, open_time ASC LIMIT 1", (symbol, interval, int(from_ms), int(to_ms)),
            ).fetchone()
            lo = conn.execute(
                "SELECT open_time, low FROM candles WHERE symbol=? AND interval=? AND open_time>=? AND open_time<=? "
                "ORDER BY low ASC, open_time ASC LIMIT 1", (symbol, interval, int(from_ms), int(to_ms)),
            ).fetchone()
        if hi is None or lo is None:
            return None
        return {"high": float(hi[1]), "high_ms": int(hi[0]), "low": float(lo[1]), "low_ms": int(lo[0])}

    def first_cross(self, symbol: str, interval: str, from_ms: int, to_ms: int, above: float | None, below: float | None) -> dict:
        """Open time of the first candle (from_ms <= open_time <= to_ms) whose high reaches `above`
        and of the first whose low reaches `below`; None where there is none or no level."""
        out: dict[str, int | None] = {"above_ms": None, "below_ms": None}
        with self._conn() as conn:
            for key, col, op, level in (("above_ms", "high", ">=", above), ("below_ms", "low", "<=", below)):
                if level is None:
                    continue
                row = conn.execute(
                    f"SELECT open_time FROM candles WHERE symbol=? AND interval=? AND open_time>=? AND open_time<=? AND {col} {op} ? "
                    "ORDER BY open_time ASC LIMIT 1", (symbol, interval, int(from_ms), int(to_ms), float(level)),
                ).fetchone()
                out[key] = int(row[0]) if row else None
        return out

    def stats(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT symbol, interval, COUNT(*), MIN(open_time), MAX(open_time) "
                "FROM candles GROUP BY symbol, interval ORDER BY symbol, interval"
            ).fetchall()
        return [
            {"symbol": s, "interval": i, "count": int(n), "first_ms": int(a), "last_ms": int(b)}
            for s, i, n, a, b in rows
        ]
