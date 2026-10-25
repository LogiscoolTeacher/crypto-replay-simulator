"""SQLite persistence for the day-trading mode: per-session settings, the daily trend checks, the
user-drawn support/resistance zones and the trades.

The rows hang off the shared `sessions` table (see session_store.py) and are deleted with it.
Like SessionStore, every method takes an open connection from `tx()` so one user action commits
atomically; the day-trading engine opens the transaction.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
-- One row per day-trading session: the fixed risk per trade and where the replay stands.
-- phase: "trading" (stepping through candles) or "exited" (a trade has just closed: end the day,
-- the jump to the next 00:00, or keep trading). start_cursor_ms: the first day's start candle (set once).
CREATE TABLE IF NOT EXISTS day_sessions (
    session_id       TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    timezone         TEXT    NOT NULL,
    risk_pct         REAL    NOT NULL,
    risk_amount      REAL    NOT NULL,
    phase            TEXT    NOT NULL,
    start_cursor_ms  INTEGER,
    last_exit_id     INTEGER,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

-- The trend check of one trading day (the UTC date, 00:00 to 24:00; 06:00 to 06:00 until 2026-10-25).
-- ema_above is computed (1/0, NULL = EMA 200 not available); hh_hl was the user's daily answer
-- (retired 2026-10-24, old rows keep it, nothing reads it).
CREATE TABLE IF NOT EXISTS day_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    day_date      TEXT    NOT NULL,
    day_start_ms  INTEGER NOT NULL,
    day_end_ms    INTEGER NOT NULL,
    daily_close   REAL,
    daily_close_ms INTEGER,
    ema200        REAL,
    ema_above     INTEGER,
    hh_hl         INTEGER,
    notes         TEXT,
    image_path    TEXT,
    image_ms      INTEGER,
    left_ms       INTEGER,
    left_how      TEXT,
    updated_at    TEXT    NOT NULL,
    UNIQUE(session_id, day_date)
);

-- Support/resistance zones drawn by the user: a horizontal band between two prices.
CREATE TABLE IF NOT EXISTS day_zones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    interval    TEXT    NOT NULL,
    upper       REAL    NOT NULL,
    lower       REAL    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS day_zones_session ON day_zones(session_id, id);

-- One row per trade (open or closed); see the spec's "Saved per trade" table.
CREATE TABLE IF NOT EXISTS day_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    status          TEXT    NOT NULL,
    day_date        TEXT    NOT NULL,
    ema_above       INTEGER,
    hh_hl           INTEGER,
    entry_ms        INTEGER NOT NULL,
    entry_price     REAL    NOT NULL,
    entry_rsi       REAL,
    entry_atr       REAL,
    zone_id         INTEGER,
    zone_upper      REAL,
    zone_lower      REAL,
    stop_suggested  REAL,
    stop_atr        REAL,
    stop_used       REAL    NOT NULL,
    take_profit     REAL    NOT NULL,
    rr              REAL,
    coins           REAL    NOT NULL,
    position_value  REAL    NOT NULL,
    risk_usdt       REAL    NOT NULL,
    risk_amount     REAL    NOT NULL,
    capped          INTEGER NOT NULL,
    buy_fee         REAL    NOT NULL,
    cash_after_entry REAL   NOT NULL,
    exit_ms         INTEGER,
    exit_price      REAL,
    exit_type       TEXT,
    exit_reason     TEXT,
    exit_note       TEXT,
    overnight       INTEGER NOT NULL DEFAULT 0,
    sell_fee        REAL,
    fees            REAL,
    pnl             REAL,
    pnl_pct         REAL,
    r_multiple      REAL,
    cash_after_exit REAL,
    whatif_n        INTEGER,
    whatif_high     REAL,
    whatif_low      REAL,
    whatif_above_exit_pct    REAL,
    whatif_below_exit_pct    REAL,
    whatif_high_vs_entry_pct REAL,
    whatif_low_vs_entry_pct  REAL,
    image_path      TEXT,
    notes           TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS day_trades_session ON day_trades(session_id, seq);
-- the automatic-exit twin of every manual trade: same entry and size, the ORIGINAL stop and
-- target, closed only when one of them is hit (overnight and across days); never touches the cash
CREATE TABLE IF NOT EXISTS day_auto_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    trade_id        INTEGER NOT NULL REFERENCES day_trades(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    status          TEXT    NOT NULL,
    day_date        TEXT    NOT NULL,
    entry_ms        INTEGER NOT NULL,
    entry_price     REAL    NOT NULL,
    stop            REAL    NOT NULL,
    take_profit     REAL    NOT NULL,
    coins           REAL    NOT NULL,
    position_value  REAL    NOT NULL,
    buy_fee         REAL    NOT NULL,
    risk_amount     REAL    NOT NULL,
    exit_ms         INTEGER,
    exit_price      REAL,
    exit_type       TEXT,
    sell_fee        REAL,
    fees            REAL,
    pnl             REAL,
    pnl_pct         REAL,
    r_multiple      REAL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS day_auto_trades_session ON day_auto_trades(session_id, seq);
"""

TABLES = ("day_sessions", "day_checks", "day_zones", "day_trades", "day_auto_trades")
# Columns added after the first release, created on databases that predate them (additive only).
MIGRATIONS = {
    "day_trades": {
        "stop_atr": "REAL",
        # the entry conditions at the buy, for the later evaluation (None = unknown at the time)
        "uptrend": "INTEGER", "price_in_zone": "INTEGER", "rsi_dipped": "INTEGER", "rsi_day_min": "REAL",
        "prev_high": "REAL", "close_above_prev_high": "INTEGER", "zone_interval": "TEXT",
        # the stop / take-profit at the buy and every later move (JSON list), see DayEngine.move_trade
        "stop_initial": "REAL", "tp_initial": "REAL", "changes": "TEXT",
        # how confident the trader was at the buy, 0 (not at all) to 10 (certain); set once, never changed
        "confidence": "INTEGER",
        # the three review marks answered at 24:00 of the exit day (0 / 1) and when they were answered
        "review_stop_tight": "INTEGER", "review_over_target": "INTEGER", "review_stop_saved": "INTEGER", "reviewed_at": "TEXT",
        # the fourth review mark, "I moved the stop too far up" (added 2026-10-25; NULL on trades reviewed before)
        "review_stop_moved_up": "INTEGER",
        # RSI 14 of the 1-hour chart at the buy (the 15m one is entry_rsi)
        "entry_rsi_1h": "REAL",
        # maximum adverse / favourable excursion between the entry and the exit, percent of the entry and in R
        "mae_pct": "REAL", "mfe_pct": "REAL", "mae_r": "REAL", "mfe_r": "REAL",
        # the two hands-off alternatives (JSON: end_ms, alt1, alt2), filled once the replay reached their 24:00
        "alts": "TEXT",
        # exits A and C (JSON: {"A": leg | null, "C": leg | null}, null = still open; NULL column = both
        # open), each a whole-size version of the exit; the trade closes with the later one (see DayEngine.sell)
        "exits_ac": "TEXT",
        # 1 = the review is asked once the replay reaches 24:00 of the exit's trading day (exits from
        # this rule on; older trades keep NULL and never hold the replay)
        "review_due": "INTEGER",
    },
    # the earliest candle "Step back" may return to, set by every jump (the candles jumped over were never seen)
    "day_sessions": {"back_floor_ms": "INTEGER"},
    # the daily journal: notes, the end-of-day picture and how / when the day was left
    # who made a zone: "signal" = the "Next signal" button (removed again while no trade uses it), NULL = drawn by hand
    "day_zones": {"source": "TEXT"},
    "day_checks": {"notes": "TEXT", "image_path": "TEXT", "image_ms": "INTEGER", "left_ms": "INTEGER", "left_how": "TEXT"},
    # the open time of the last candle a twin was checked against (the settlement only reads newer ones)
    "day_auto_trades": {"checked_ms": "INTEGER"},
}


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if r is None else dict(r)


class DayStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as conn:
            conn.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                for col, typ in cols.items():
                    if col not in have:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            self._columns = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in TABLES}

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

    def upgrade_risk(self, conn: sqlite3.Connection, risk_pct: float) -> int:
        """Bring sessions made under an older risk rule to `risk_pct` (of the starting capital) by
        raising their starting capital and cash, never their risk amount: 10k at 1 % (100 USDT)
        becomes 100k at 0.1 % (still 100 USDT). Trades keep their numbers; their cash snapshots
        move by the same amount. Only raises; idempotent (risk_pct is the marker). Returns the
        number of sessions changed."""
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone() is None:
            return 0
        rows = conn.execute(
            "SELECT d.session_id, d.risk_amount, s.start_balance FROM day_sessions d JOIN sessions s ON s.id = d.session_id "
            "WHERE abs(d.risk_pct - ?) > 1e-12", (risk_pct,)).fetchall()
        changed = 0
        for r in rows:
            delta = r["risk_amount"] * 100.0 / risk_pct - r["start_balance"]
            if delta > 1e-9:
                conn.execute("UPDATE sessions SET start_balance = start_balance + ?, cash = cash + ? WHERE id = ?", (delta, delta, r["session_id"]))
                conn.execute("UPDATE day_trades SET cash_after_entry = cash_after_entry + ?, "
                             "cash_after_exit = CASE WHEN cash_after_exit IS NULL THEN NULL ELSE cash_after_exit + ? END "
                             "WHERE session_id = ?", (delta, delta, r["session_id"]))
                changed += 1
            conn.execute("UPDATE day_sessions SET risk_pct = ? WHERE session_id = ?", (risk_pct, r["session_id"]))
        return changed

    # ---------------------------------------------------------------- generic
    def _insert(self, conn: sqlite3.Connection, table: str, f: dict[str, Any]) -> int:
        bad = set(f) - set(self._columns[table])
        if bad:
            raise ValueError(f"unknown {table} fields {sorted(bad)}")
        cols = ", ".join(f)
        marks = ", ".join("?" for _ in f)
        cur = conn.execute(f"INSERT INTO {table}({cols}) VALUES ({marks})", list(f.values()))
        return int(cur.lastrowid)

    def _update(self, conn: sqlite3.Connection, table: str, key: str, key_value: Any, f: dict[str, Any]) -> None:
        bad = set(f) - set(self._columns[table])
        if bad or not f:
            raise ValueError(f"cannot update {table} fields {sorted(bad)}")
        sets = ", ".join(f"{k}=?" for k in f)
        cur = conn.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", [*f.values(), key_value])
        if cur.rowcount != 1:
            raise KeyError(key_value)

    # ----------------------------------------------------------- day sessions
    def create_day_session(self, conn: sqlite3.Connection, **f: Any) -> dict:
        self._insert(conn, "day_sessions", f)
        return self.get_day_session(conn, f["session_id"])  # type: ignore[return-value]

    def get_day_session(self, conn: sqlite3.Connection, session_id: str) -> dict | None:
        return _row(conn.execute("SELECT * FROM day_sessions WHERE session_id=?", (session_id,)).fetchone())

    def update_day_session(self, conn: sqlite3.Connection, session_id: str, **f: Any) -> None:
        self._update(conn, "day_sessions", "session_id", session_id, f)

    # ------------------------------------------------------------------ checks
    def add_check(self, conn: sqlite3.Connection, **f: Any) -> dict:
        cid = self._insert(conn, "day_checks", f)
        return _row(conn.execute("SELECT * FROM day_checks WHERE id=?", (cid,)).fetchone())  # type: ignore[return-value]

    def get_check(self, conn: sqlite3.Connection, session_id: str, day_date: str) -> dict | None:
        return _row(conn.execute(
            "SELECT * FROM day_checks WHERE session_id=? AND day_date=?", (session_id, day_date)
        ).fetchone())

    def list_checks(self, conn: sqlite3.Connection, session_id: str) -> list[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM day_checks WHERE session_id=? ORDER BY day_date", (session_id,)).fetchall()]

    def update_check(self, conn: sqlite3.Connection, check_id: int, **f: Any) -> None:
        self._update(conn, "day_checks", "id", check_id, f)

    def delete_checks(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute("DELETE FROM day_checks WHERE session_id=?", (session_id,))

    # ------------------------------------------------------------------- zones
    def add_zone(self, conn: sqlite3.Connection, **f: Any) -> dict:
        zid = self._insert(conn, "day_zones", f)
        return self.get_zone(conn, f["session_id"], zid)  # type: ignore[return-value]

    def get_zone(self, conn: sqlite3.Connection, session_id: str, zone_id: int) -> dict | None:
        return _row(conn.execute("SELECT * FROM day_zones WHERE session_id=? AND id=?", (session_id, zone_id)).fetchone())

    def list_zones(self, conn: sqlite3.Connection, session_id: str) -> list[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM day_zones WHERE session_id=? ORDER BY id", (session_id,)).fetchall()]

    def update_zone(self, conn: sqlite3.Connection, zone_id: int, **f: Any) -> None:
        self._update(conn, "day_zones", "id", zone_id, f)

    def delete_zone(self, conn: sqlite3.Connection, session_id: str, zone_id: int) -> bool:
        return conn.execute("DELETE FROM day_zones WHERE session_id=? AND id=?", (session_id, zone_id)).rowcount == 1

    def delete_unused_signal_zones(self, conn: sqlite3.Connection, session_id: str, keep: int | None = None) -> int:
        """Delete the session's "signal" zones that no trade (open or closed) uses, except `keep`."""
        return conn.execute(
            "DELETE FROM day_zones WHERE session_id=? AND source='signal' AND id IS NOT ? "
            "AND id NOT IN (SELECT zone_id FROM day_trades WHERE session_id=? AND zone_id IS NOT NULL)",
            (session_id, keep, session_id)).rowcount

    def delete_zones(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute("DELETE FROM day_zones WHERE session_id=?", (session_id,))

    # ------------------------------------------------------------------ trades
    def add_trade(self, conn: sqlite3.Connection, **f: Any) -> dict:
        n = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM day_trades WHERE session_id=?", (f["session_id"],)).fetchone()[0]
        tid = self._insert(conn, "day_trades", {**f, "seq": int(n) + 1})
        return self.get_trade(conn, f["session_id"], tid)  # type: ignore[return-value]

    def get_trade(self, conn: sqlite3.Connection, session_id: str, trade_id: int) -> dict | None:
        return _row(conn.execute("SELECT * FROM day_trades WHERE session_id=? AND id=?", (session_id, trade_id)).fetchone())

    def open_trade(self, conn: sqlite3.Connection, session_id: str) -> dict | None:
        return _row(conn.execute(
            "SELECT * FROM day_trades WHERE session_id=? AND status='open' ORDER BY id DESC LIMIT 1", (session_id,)
        ).fetchone())

    def list_trades(self, conn: sqlite3.Connection, session_id: str) -> list[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM day_trades WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()]

    def closed_trades(self, conn: sqlite3.Connection, session_id: str | None = None) -> list[dict]:
        """Closed trades of one session, or of every day session (`session_id` None), oldest first."""
        if session_id is None:
            return [dict(r) for r in conn.execute("SELECT * FROM day_trades WHERE status='closed' ORDER BY exit_ms, id").fetchall()]
        return [dict(r) for r in conn.execute(
            "SELECT * FROM day_trades WHERE session_id=? AND status='closed' ORDER BY exit_ms, id", (session_id,)
        ).fetchall()]

    def due_reviews(self, conn: sqlite3.Connection, session_id: str) -> list[dict]:
        """Closed trades whose review is still to be answered, oldest exit first."""
        return [dict(r) for r in conn.execute(
            "SELECT * FROM day_trades WHERE session_id=? AND status='closed' AND review_due=1 AND reviewed_at IS NULL"
            " ORDER BY exit_ms, id", (session_id,)
        ).fetchall()]

    def trades_on_day(self, conn: sqlite3.Connection, session_id: str, day_date: str) -> int:
        return int(conn.execute(
            "SELECT COUNT(*) FROM day_trades WHERE session_id=? AND day_date=?", (session_id, day_date)
        ).fetchone()[0])

    def update_trade(self, conn: sqlite3.Connection, trade_id: int, **f: Any) -> None:
        self._update(conn, "day_trades", "id", trade_id, f)

    def delete_trades(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute("DELETE FROM day_trades WHERE session_id=?", (session_id,))

    # ------------------------------------------------------- automatic twins
    def add_auto_trade(self, conn: sqlite3.Connection, **f: Any) -> dict:
        aid = self._insert(conn, "day_auto_trades", f)
        return _row(conn.execute("SELECT * FROM day_auto_trades WHERE id=?", (aid,)).fetchone())  # type: ignore[return-value]

    def auto_trade_of(self, conn: sqlite3.Connection, session_id: str, trade_id: int) -> dict | None:
        return _row(conn.execute("SELECT * FROM day_auto_trades WHERE session_id=? AND trade_id=?", (session_id, trade_id)).fetchone())

    def open_auto_trades(self, conn: sqlite3.Connection, session_id: str) -> list[dict]:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM day_auto_trades WHERE session_id=? AND status='open' ORDER BY seq", (session_id,)).fetchall()]

    def list_auto_trades(self, conn: sqlite3.Connection, session_id: str) -> list[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM day_auto_trades WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()]

    def update_auto_trade(self, conn: sqlite3.Connection, auto_id: int, **f: Any) -> None:
        self._update(conn, "day_auto_trades", "id", auto_id, f)

    def delete_auto_trades(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute("DELETE FROM day_auto_trades WHERE session_id=?", (session_id,))
