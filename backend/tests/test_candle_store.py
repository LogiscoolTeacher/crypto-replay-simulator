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
    assert list(df.columns) == ["time", "open", "high", "low", "close", "volume"]


def test_rejects_unknown_symbol_and_interval(db):
    store = CandleStore(db, FakeClient(LISTING + 10 * H))
    with pytest.raises(ValueError):
        store.ensure_range("FOOUSDT", "1h", LISTING)
    with pytest.raises(ValueError):
        store.ensure_range("BTCUSDT", "2h", LISTING)
