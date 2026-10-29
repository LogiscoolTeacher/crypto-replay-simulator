"""Unit tests for CandleStore using a fake Binance client (no network)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.data.candle_store import CandleStore, last_closed_open_time
from tests.fakes import H, LISTING, FakeClient


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def test_last_closed_open_time():
    now = LISTING + 10 * H + 1234
    assert last_closed_open_time(now, H) == LISTING + 9 * H
    # Exactly on a boundary: the candle that just closed is the previous slot.
    assert last_closed_open_time(LISTING + 10 * H, H) == LISTING + 9 * H


def test_download_drops_forming_candle_and_pages(db):
    now = LISTING + 2500 * H + 5000  # 2500 closed candles + one forming
    client = FakeClient(now)
    store = CandleStore(db, client)
    added = store.ensure_range("BTCUSDT", "1h", LISTING)
    assert added == 2500
    df = store.get_candles("BTCUSDT", "1h")
    assert len(df) == 2500
    assert df["time"].iloc[0] == LISTING // 1000
    assert df["time"].iloc[-1] == (LISTING + 2499 * H) // 1000
    assert df["time"].is_monotonic_increasing and df["time"].is_unique
    assert len(client.calls) == 3  # 1000 + 1000 + 500
    # Whole range is now covered; a second call costs no requests.
    client.calls.clear()
    assert store.ensure_range("BTCUSDT", "1h", LISTING) == 0
    assert client.calls == []


def test_covered_range_needs_no_network(db, monkeypatch):
    """A range already downloaded is served without asking Binance anything, not even the server
    time, so preparing an old session works offline."""
    client = FakeClient(LISTING + 50 * H)
    store = CandleStore(db, client)
    assert store.ensure_range("BTCUSDT", "1h", LISTING, LISTING + 40 * H) == 41
    client.calls.clear()

    def offline():
        raise AssertionError("no server time needed")
    monkeypatch.setattr(client, "server_time_ms", offline)
    assert store.ensure_range("BTCUSDT", "1h", LISTING, LISTING + 40 * H) == 0
    assert store.ensure_range("BTCUSDT", "1h", LISTING + 10 * H, LISTING + 20 * H) == 0
    assert client.calls == []


def test_pre_listing_window_is_marked_covered(db):
    now = LISTING + 50 * H
    client = FakeClient(now)
    store = CandleStore(db, client)
    early = LISTING - 1000 * H
    added = store.ensure_range("BTCUSDT", "1h", early, LISTING + 9 * H)
    assert added == 10
    assert store.coverage("BTCUSDT", "1h") == [(early, LISTING + 9 * H)]
    client.calls.clear()
    assert store.ensure_range("BTCUSDT", "1h", early, LISTING + 9 * H) == 0
    assert client.calls == []


def test_real_gap_is_not_refetched(db):
    now = LISTING + 50 * H
    hole = LISTING + 20 * H
    client = FakeClient(now, holes={hole})
    store = CandleStore(db, client)
    added = store.ensure_range("BTCUSDT", "1h", LISTING, LISTING + 40 * H)
    assert added == 40  # 41 slots minus the hole
    client.calls.clear()
    assert store.ensure_range("BTCUSDT", "1h", LISTING, LISTING + 40 * H) == 0
    assert client.calls == []


def test_missing_ranges_and_merge(db):
    now = LISTING + 100 * H
    client = FakeClient(now)
    store = CandleStore(db, client)
    store.ensure_range("BTCUSDT", "1h", LISTING + 10 * H, LISTING + 19 * H)
    store.ensure_range("BTCUSDT", "1h", LISTING + 30 * H, LISTING + 39 * H)
    gaps = store.missing_ranges("BTCUSDT", "1h", LISTING, LISTING + 50 * H)
    assert gaps == [
        (LISTING, LISTING + 9 * H),
        (LISTING + 20 * H, LISTING + 29 * H),
        (LISTING + 40 * H, LISTING + 50 * H),
    ]
    # Filling the middle gap merges all three ranges into one.
    store.ensure_range("BTCUSDT", "1h", LISTING + 20 * H, LISTING + 29 * H)
    assert store.coverage("BTCUSDT", "1h") == [(LISTING + 10 * H, LISTING + 39 * H)]
    # Unaligned request bounds are floored to slots.
    assert store.missing_ranges("BTCUSDT", "1h", LISTING + 10 * H + 1, LISTING + 39 * H + 999) == []


def test_get_candles_window_and_limit(db):
    now = LISTING + 100 * H
    store = CandleStore(db, FakeClient(now))
    store.ensure_range("BTCUSDT", "1h", LISTING)
    df = store.get_candles("BTCUSDT", "1h", start_ms=LISTING + 10 * H, end_ms=LISTING + 19 * H)
    assert len(df) == 10
    df = store.get_candles("BTCUSDT", "1h", end_ms=LISTING + 19 * H, limit=3)
    assert list(df["time"]) == [(LISTING + k * H) // 1000 for k in (17, 18, 19)]
    assert list(df.columns) == ["time", "open", "high", "low", "close", "volume", "taker_buy"]
    assert df["taker_buy"].tolist() == [6.0, 6.0, 6.0]


def test_old_database_gets_the_taker_buy_column_and_a_backfill(db):
    """A database from before the volume delta: the column is added on open, old rows read NaN,
    and fill_taker_buy downloads them again with the taker-buy volume (and only those)."""
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE candles (symbol TEXT NOT NULL, interval TEXT NOT NULL, open_time INTEGER NOT NULL, "
                 "open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, "
                 "PRIMARY KEY (symbol, interval, open_time)) WITHOUT ROWID")
    conn.executemany("INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)",
                     [("BTCUSDT", "1h", LISTING + k * H, 1.0, 2.0, 0.5, 1.5, 10.0) for k in range(5)])
    conn.commit()
    conn.close()
    client = FakeClient(LISTING + 50 * H)
    store = CandleStore(db, client)
    df = store.get_candles("BTCUSDT", "1h")
    assert len(df) == 5 and df["taker_buy"].isna().all()
    assert store.missing_taker_buy("BTCUSDT", "1h", LISTING, LISTING + 10 * H) == (LISTING, LISTING + 4 * H)
    assert store.fill_taker_buy("BTCUSDT", "1h", LISTING + H, LISTING + 3 * H) == 3
    df = store.get_candles("BTCUSDT", "1h")
    assert df["taker_buy"].isna().tolist() == [True, False, False, False, True]
    assert df["close"].tolist() == [1.5] * 5            # only the taker-buy volume is written
    assert store.fill_taker_buy("BTCUSDT", "1h", LISTING + H, LISTING + 3 * H) == 0   # nothing left there
    assert store.fill_taker_buy_soon("BTCUSDT", "1h", LISTING + H, LISTING + 3 * H) is False


def test_taker_buy_backfill_failure_waits_before_the_next_try(db):
    import threading
    client = FakeClient(LISTING + 50 * H)
    store = CandleStore(db, client)
    store.ensure_range("BTCUSDT", "1h", LISTING, LISTING + 9 * H)
    with store._conn() as conn:
        conn.execute("UPDATE candles SET taker_buy=NULL")
    done = threading.Event()

    def offline(*a, **k):
        done.set()
        raise OSError("offline")
    client.klines = offline
    assert store.fill_taker_buy_soon("BTCUSDT", "1h", LISTING, LISTING + 9 * H) is True
    assert done.wait(5)
    for _ in range(100):   # the thread records the failure right after the call
        if store._fill_failed:
            break
        threading.Event().wait(0.01)
    assert store.fill_taker_buy_soon("BTCUSDT", "1h", LISTING, LISTING + 9 * H) is False   # waits retry_s
    assert store.fill_taker_buy_soon("BTCUSDT", "1h", LISTING, LISTING + 9 * H, retry_s=0) is True


def test_rejects_unknown_symbol_and_interval(db):
    store = CandleStore(db, FakeClient(LISTING + 10 * H))
    with pytest.raises(ValueError):
        store.ensure_range("FOOUSDT", "1h", LISTING)
    with pytest.raises(ValueError):
        store.ensure_range("BTCUSDT", "2h", LISTING)
