"""SQLite persistence for replay sessions, buckets and trades.

All methods take an open connection from `tx()` so that one user action
(e.g. a buy = bucket row + trade row + cash update) commits atomically.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    name           TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    base_interval  TEXT    NOT NULL,
    start_ms       INTEGER NOT NULL,
    end_ms         INTEGER NOT NULL,
    data_start_ms  INTEGER,
    data_end_ms    INTEGER,
    cursor_ms      INTEGER,
    start_balance  REAL    NOT NULL,
    cash           REAL    NOT NULL,
    fee_rate       REAL    NOT NULL,
    status         TEXT    NOT NULL,
    error          TEXT,
    start_note     TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS buckets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    status       TEXT    NOT NULL,
    opened_ms    INTEGER NOT NULL,
    entry_price  REAL    NOT NULL,
    qty          REAL    NOT NULL,
    cost         REAL    NOT NULL,
    buy_fee      REAL    NOT NULL,
    closed_ms    INTEGER,
    exit_price   REAL,
    proceeds     REAL,
    sell_fee     REAL,
    realized_pnl REAL,
    buy_rsi      REAL,
    buy_rsi_interval  TEXT,
    sell_rsi     REAL,
    sell_rsi_interval TEXT,
    buy_ema200   REAL,
    buy_sma200   REAL,
    buy_ema200_pct  REAL,
    buy_sma200_pct  REAL,
    sell_ema200  REAL,
    sell_sma200  REAL,
    sell_ema200_pct REAL,
    sell_sma200_pct REAL,
    held_high    REAL,
    held_high_ms INTEGER,
    held_low     REAL,
    held_low_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS buckets_session ON buckets(session_id, status);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    bucket_id   INTEGER NOT NULL,
    side        TEXT    NOT NULL,
    time_ms     INTEGER NOT NULL,
    price       REAL    NOT NULL,
    qty         REAL    NOT NULL,
    quote       REAL    NOT NULL,
    fee         REAL    NOT NULL,
    cash_after  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_session ON trades(session_id, id);

-- Fibonacci retracements drawn by the user: two anchors (first click = 100 %, second = 0 %).
CREATE TABLE IF NOT EXISTS fibs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    interval   TEXT    NOT NULL,
    t1_ms      INTEGER NOT NULL,
    p1         REAL    NOT NULL,
    t2_ms      INTEGER NOT NULL,
    p2         REAL    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS fibs_session ON fibs(session_id, id);

-- Trade journal: one row per buy and per sell with the readings at that moment and a chart picture.
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    bucket_id  INTEGER NOT NULL,
    side       TEXT    NOT NULL,
    time_ms    INTEGER NOT NULL,
    interval   TEXT    NOT NULL,
    image_path TEXT,
    readings   TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE(bucket_id, side)
);
CREATE INDEX IF NOT EXISTS snapshots_session ON snapshots(session_id, id);
"""

SESSION_FIELDS = (
    "id", "name", "symbol", "base_interval", "start_ms", "end_ms", "data_start_ms", "data_end_ms",
    "cursor_ms", "start_balance", "cash", "fee_rate", "status", "error", "start_note", "created_at", "updated_at", "mode",
)
# Session columns added after the first release; older databases get them on open.
# start_note: why the replay starts later than asked, or a warning. mode: "swing" (buckets, the default
# for rows from before the column existed) or "day" (day-trading mode, see day_engine.py).
SESSION_MIGRATIONS = {"start_note": "TEXT", "mode": "TEXT"}
BUCKET_FIELDS = (
    "session_id", "seq", "status", "opened_ms", "entry_price", "qty", "cost", "buy_fee",
    "closed_ms", "exit_price", "proceeds", "sell_fee", "realized_pnl",
    "buy_rsi", "buy_rsi_interval", "sell_rsi", "sell_rsi_interval",
    *[f"{side}_{col}" for side in ("buy", "sell") for col in ("ema200", "sma200", "ema200_pct", "sma200_pct")],
    "held_high", "held_high_ms", "held_low", "held_low_ms",
    "part", "part_pct", "parent_id",
    "tp_pct", "sl_pct", "tp_price", "sl_price", "armed_ms", "trigger",
)
# Target and stop set at the buy (percent of the entry price and the resulting prices), when the
# automatic sell was switched on (replay time; NULL = never) and, on a closed row, whether the
# sell was triggered by the "target" or the "stop" (NULL = sold by hand).
PLAN_COLUMNS = {"tp_pct": "REAL", "sl_pct": "REAL", "tp_price": "REAL", "sl_price": "REAL", "armed_ms": "INTEGER", "trigger": "TEXT"}
# A bucket sold in parts: every part sold before the last is its own closed row with the bucket's
# number (`seq`), the part number, the percent of what was left that it took and the open bucket
# it came from; the bucket itself closes as the last part.
PART_COLUMNS = {"part": "INTEGER", "part_pct": "REAL", "parent_id": "INTEGER"}
# Highest high and lowest low of the base candles while a closed bucket was held (filled at the sell,
# or on first listing for buckets closed before these columns existed).
EXCURSION_COLUMNS = {"held_high": "REAL", "held_high_ms": "INTEGER", "held_low": "REAL", "held_low_ms": "INTEGER"}
# Daily EMA/SMA 200 and the price's distance to them at the buy and at the sell, for the History tab.
LONG_AVG_COLUMNS = tuple(f"{side}_{col}" for side in ("buy", "sell") for col in ("ema200", "sma200", "ema200_pct", "sma200_pct"))
# Columns added after the first release; older databases get them on open.
BUCKET_MIGRATIONS = {
    "buy_rsi": "REAL", "buy_rsi_interval": "TEXT", "sell_rsi": "REAL", "sell_rsi_interval": "TEXT",
    **{col: "REAL" for col in LONG_AVG_COLUMNS},
    **EXCURSION_COLUMNS,
    **PART_COLUMNS,
    **PLAN_COLUMNS,
}
TRADE_FIELDS = ("session_id", "bucket_id", "side", "time_ms", "price", "qty", "quote", "fee", "cash_after")
FIB_FIELDS = ("session_id", "interval", "t1_ms", "p1", "t2_ms", "p2", "created_at")
SNAPSHOT_FIELDS = ("session_id", "bucket_id", "side", "time_ms", "interval", "image_path", "readings", "created_at")


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if r is None else dict(r)


class SessionStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as conn:
            conn.executescript(SCHEMA)
            have_s = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            for col, typ in SESSION_MIGRATIONS.items():
                if col not in have_s:
                    conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typ}")
            have = {r[1] for r in conn.execute("PRAGMA table_info(buckets)")}
            for col, typ in BUCKET_MIGRATIONS.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE buckets ADD COLUMN {col} {typ}")
            if any(col not in have for col in LONG_AVG_COLUMNS):
                self._backfill_long_averages(conn)

    @staticmethod
    def _backfill_long_averages(conn: sqlite3.Connection) -> None:
        """Buckets from before the EMA/SMA columns existed: copy the values their journal rows recorded."""
        rows = conn.execute("SELECT bucket_id, side, readings FROM snapshots WHERE side IN ('buy', 'sell')").fetchall()
        for r in rows:
            try:
                d = json.loads(r["readings"])
            except (TypeError, ValueError):
                continue
            p = r["side"]
            conn.execute(
                f"UPDATE buckets SET {p}_ema200=?, {p}_sma200=?, {p}_ema200_pct=?, {p}_sma200_pct=? WHERE id=? AND {p}_ema200 IS NULL",
                (d.get("ema200"), d.get("sma200"), d.get("ema200_pct"), d.get("sma200_pct"), r["bucket_id"]),
            )

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------------- sessions
    def create_session(self, conn: sqlite3.Connection, **f: Any) -> dict:
        missing = set(SESSION_FIELDS) - set(f)
        if missing:
            raise ValueError(f"session fields missing: {sorted(missing)}")
        cols = ", ".join(SESSION_FIELDS)
        marks = ", ".join("?" for _ in SESSION_FIELDS)
        conn.execute(f"INSERT INTO sessions({cols}) VALUES ({marks})", [f[k] for k in SESSION_FIELDS])
        return self.get_session(conn, f["id"])  # type: ignore[return-value]

    def get_session(self, conn: sqlite3.Connection, session_id: str) -> dict | None:
        return _row(conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    def list_sessions(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def update_session(self, conn: sqlite3.Connection, session_id: str, **f: Any) -> None:
        bad = set(f) - set(SESSION_FIELDS) | {"id"} & set(f)
        if bad:
            raise ValueError(f"cannot update session fields {sorted(bad)}")
        sets = ", ".join(f"{k}=?" for k in f)
        cur = conn.execute(f"UPDATE sessions SET {sets} WHERE id=?", [*f.values(), session_id])
        if cur.rowcount != 1:
            raise KeyError(session_id)

    def delete_session(self, conn: sqlite3.Connection, session_id: str) -> bool:
        return conn.execute("DELETE FROM sessions WHERE id=?", (session_id,)).rowcount == 1

    # ----------------------------------------------------------------- buckets
    def add_bucket(self, conn: sqlite3.Connection, **f: Any) -> dict:
        n = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM buckets WHERE session_id=?", (f["session_id"],)).fetchone()[0]
        f = {**{k: None for k in BUCKET_FIELDS}, "seq": int(n) + 1, **f}  # an explicit seq (a sold part) wins
        cols = ", ".join(BUCKET_FIELDS)
        marks = ", ".join("?" for _ in BUCKET_FIELDS)
        cur = conn.execute(f"INSERT INTO buckets({cols}) VALUES ({marks})", [f[k] for k in BUCKET_FIELDS])
        return self.get_bucket(conn, int(cur.lastrowid))  # type: ignore[return-value]

    def get_bucket(self, conn: sqlite3.Connection, bucket_id: int) -> dict | None:
        return _row(conn.execute("SELECT * FROM buckets WHERE id=?", (bucket_id,)).fetchone())

    def list_buckets(self, conn: sqlite3.Connection, session_id: str, status: str | None = None) -> list[dict]:
        if status is None:
            rows = conn.execute("SELECT * FROM buckets WHERE session_id=? ORDER BY seq, part", (session_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM buckets WHERE session_id=? AND status=? ORDER BY seq, part", (session_id, status)
            ).fetchall()
        return [dict(r) for r in rows]

    def update_bucket(self, conn: sqlite3.Connection, bucket_id: int, **f: Any) -> None:
        bad = set(f) - set(BUCKET_FIELDS)
        if bad:
            raise ValueError(f"cannot update bucket fields {sorted(bad)}")
        sets = ", ".join(f"{k}=?" for k in f)
        cur = conn.execute(f"UPDATE buckets SET {sets} WHERE id=?", [*f.values(), bucket_id])
        if cur.rowcount != 1:
            raise KeyError(bucket_id)

    def delete_buckets(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute("DELETE FROM buckets WHERE session_id=?", (session_id,))

    # ------------------------------------------------------------------ trades
    def add_trade(self, conn: sqlite3.Connection, **f: Any) -> dict:
        missing = set(TRADE_FIELDS) - set(f)
        if missing:
            raise ValueError(f"trade fields missing: {sorted(missing)}")
        cols = ", ".join(TRADE_FIELDS)
        marks = ", ".join("?" for _ in TRADE_FIELDS)
        cur = conn.execute(f"INSERT INTO trades({cols}) VALUES ({marks})", [f[k] for k in TRADE_FIELDS])
        return _row(conn.execute("SELECT * FROM trades WHERE id=?", (cur.lastrowid,)).fetchone())  # type: ignore[return-value]

    def list_trades(self, conn: sqlite3.Connection, session_id: str) -> list[dict]:
        rows = conn.execute("SELECT * FROM trades WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_trades(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute("DELETE FROM trades WHERE session_id=?", (session_id,))

    # -------------------------------------------------------------------- fibs
    def add_fib(self, conn: sqlite3.Connection, **f: Any) -> dict:
        missing = set(FIB_FIELDS) - set(f)
        if missing:
            raise ValueError(f"fib fields missing: {sorted(missing)}")
        cols = ", ".join(FIB_FIELDS)
        marks = ", ".join("?" for _ in FIB_FIELDS)
        cur = conn.execute(f"INSERT INTO fibs({cols}) VALUES ({marks})", [f[k] for k in FIB_FIELDS])
        return _row(conn.execute("SELECT * FROM fibs WHERE id=?", (cur.lastrowid,)).fetchone())  # type: ignore[return-value]

    def list_fibs(self, conn: sqlite3.Connection, session_id: str) -> list[dict]:
        rows = conn.execute("SELECT * FROM fibs WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_fib(self, conn: sqlite3.Connection, session_id: str, fib_id: int) -> bool:
        return conn.execute("DELETE FROM fibs WHERE session_id=? AND id=?", (session_id, fib_id)).rowcount == 1

    def delete_fibs(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute("DELETE FROM fibs WHERE session_id=?", (session_id,))

    # --------------------------------------------------------------- snapshots
    def add_snapshot(self, conn: sqlite3.Connection, **f: Any) -> dict:
        missing = set(SNAPSHOT_FIELDS) - set(f)
        if missing:
            raise ValueError(f"snapshot fields missing: {sorted(missing)}")
        cols = ", ".join(SNAPSHOT_FIELDS)
        marks = ", ".join("?" for _ in SNAPSHOT_FIELDS)
        cur = conn.execute(f"INSERT INTO snapshots({cols}) VALUES ({marks})", [f[k] for k in SNAPSHOT_FIELDS])
        return _row(conn.execute("SELECT * FROM snapshots WHERE id=?", (cur.lastrowid,)).fetchone())  # type: ignore[return-value]

    def get_snapshot(self, conn: sqlite3.Connection, session_id: str, bucket_id: int, side: str) -> dict | None:
        return _row(conn.execute(
            "SELECT * FROM snapshots WHERE session_id=? AND bucket_id=? AND side=?", (session_id, bucket_id, side)
        ).fetchone())

    def get_snapshot_by_id(self, conn: sqlite3.Connection, session_id: str, snap_id: int) -> dict | None:
        return _row(conn.execute("SELECT * FROM snapshots WHERE session_id=? AND id=?", (session_id, snap_id)).fetchone())

    def list_snapshots(self, conn: sqlite3.Connection, session_id: str) -> list[dict]:
        rows = conn.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY time_ms, id", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def update_snapshot(self, conn: sqlite3.Connection, snap_id: int, **f: Any) -> None:
        bad = set(f) - set(SNAPSHOT_FIELDS)
        if bad:
            raise ValueError(f"cannot update snapshot fields {sorted(bad)}")
        sets = ", ".join(f"{k}=?" for k in f)
        conn.execute(f"UPDATE snapshots SET {sets} WHERE id=?", [*f.values(), snap_id])

    def delete_snapshots(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute("DELETE FROM snapshots WHERE session_id=?", (session_id,))
