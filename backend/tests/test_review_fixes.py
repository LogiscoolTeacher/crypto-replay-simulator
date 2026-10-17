"""Regression tests for the defects found in the step-6 review."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.data.candle_store import CandleStore
from app.indicators.registry import compute_indicator
from app.replay.engine import COMPUTE_WARMUP_BARS, Invalid, ReplayEngine
from app.replay.session_store import SessionStore
from tests.fakes import H, LISTING, FakeClient

import pandas as pd

NOW = LISTING + 400 * H + 1


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


@pytest.fixture
def engine(tmp_path: Path) -> ReplayEngine:
    db = tmp_path / "r.db"
    return ReplayEngine(CandleStore(db, FakeClient(NOW)), SessionStore(db))


# ------------------------------------------------------------ candle store
def test_concurrent_coverage_updates_are_not_lost(tmp_path: Path):
    """Two threads downloading different ranges of the same series must both be recorded."""
    store = CandleStore(tmp_path / "c.db", FakeClient(NOW))
    ranges = [(LISTING, LISTING + 9 * H), (LISTING + 100 * H, LISTING + 109 * H), (LISTING + 200 * H, LISTING + 209 * H)]
    errors: list[Exception] = []

    def run(r):
        try:
            store.ensure_range("BTCUSDT", "1h", *r)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(r,)) for r in ranges]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store.coverage("BTCUSDT", "1h") == ranges
    assert store.missing_ranges("BTCUSDT", "1h", LISTING, LISTING + 209 * H) == [
        (LISTING + 10 * H, LISTING + 99 * H),
        (LISTING + 110 * H, LISTING + 199 * H),
    ]


def test_nth_at_or_after(tmp_path: Path):
    store = CandleStore(tmp_path / "n.db", FakeClient(NOW, holes={LISTING + 2 * H}))
    store.ensure_range("BTCUSDT", "1h", LISTING, LISTING + 10 * H)
    assert store.nth_at_or_after("BTCUSDT", "1h", LISTING, 1) == LISTING
    assert store.nth_at_or_after("BTCUSDT", "1h", LISTING, 3) == LISTING + 3 * H  # hole at +2h skipped
    assert store.nth_at_or_after("BTCUSDT", "1h", LISTING, 10) == LISTING + 10 * H
    assert store.nth_at_or_after("BTCUSDT", "1h", LISTING, 11) is None
    with pytest.raises(ValueError):
        store.nth_at_or_after("BTCUSDT", "1h", LISTING, 0)


# ----------------------------------------------------------------- engine
def test_big_step_is_one_query_and_clamps(engine: ReplayEngine):
    s = engine.prepare(engine.create_session("BTCUSDT", LISTING + 100 * H, LISTING + 300 * H)["id"])
    engine.candles.client.calls.clear()
    st = engine.step(s["id"], n=150)
    assert st["cursor_ms"] == LISTING + 250 * H
    st = engine.step(s["id"], interval="4h", n=5)  # 250h is in [248h, 252h): 5 boundaries -> 268h, land on 267h
    assert st["cursor_ms"] == LISTING + 267 * H
    st = engine.step(s["id"], interval="1D", n=100_000)
    assert st["cursor_ms"] == LISTING + 300 * H and st["finished"]


def test_prepare_marks_preparing_and_clears_error(engine: ReplayEngine):
    s = engine.create_session("BTCUSDT", LISTING - 50 * H, LISTING - 10 * H)
    with pytest.raises(Invalid):
        engine.prepare(s["id"])
    assert engine.state(s["id"])["status"] == "error"
    # Widen the window through the store and retry: the error must clear.
    with engine.sessions.tx() as conn:
        engine.sessions.update_session(conn, s["id"], end_ms=LISTING + 10 * H)
    st = engine.prepare(s["id"])
    assert st["status"] == "ready" and st["error"] is None


@pytest.mark.parametrize("kwargs", [
    {"start_balance": float("inf")},
    {"start_balance": float("nan")},
    {"start_balance": 1e13},
    {"fee_rate": float("nan")},
])
def test_create_session_rejects_non_finite(engine: ReplayEngine, kwargs):
    with pytest.raises(Invalid):
        engine.create_session("BTCUSDT", LISTING + 100 * H, LISTING + 300 * H, **kwargs)


def test_create_session_rejects_prehistoric_start(engine: ReplayEngine):
    with pytest.raises(Invalid):
        engine.create_session("BTCUSDT", -10_000_000_000, LISTING)


def test_buy_rejects_non_finite(engine: ReplayEngine):
    s = engine.prepare(engine.create_session("BTCUSDT", LISTING + 100 * H, LISTING + 300 * H)["id"])
    for bad in (float("inf"), float("nan")):
        with pytest.raises(Invalid):
            engine.buy(s["id"], bad)


def test_view_window_is_bounded_but_stable(engine: ReplayEngine):
    """The compute window never exceeds limit + COMPUTE_WARMUP_BARS rows, and values stay put."""
    s = engine.prepare(engine.create_session("BTCUSDT", LISTING + 100 * H, LISTING + 300 * H)["id"])
    captured: list[int] = []
    original = engine.candles.get_candles

    def spy(*args, **kwargs):
        df = original(*args, **kwargs)
        captured.append(len(df))
        return df

    engine.candles.get_candles = spy  # type: ignore[method-assign]
    engine.step(s["id"], n=100)
    v1 = engine.view(s["id"], "1h", limit=20, indicators=[("ema", {"period": 20})])
    assert max(captured) <= 20 + COMPUTE_WARMUP_BARS
    engine.step(s["id"], n=50)
    v2 = engine.view(s["id"], "1h", limit=70, indicators=[("ema", {"period": 20})])
    e1 = dict(zip(v1["time"], v1["indicators"][0]["outputs"]["ema"]))
    e2 = dict(zip(v2["time"], v2["indicators"][0]["outputs"]["ema"]))
    for t, val in e1.items():
        assert e2[t] == pytest.approx(val)


# -------------------------------------------------------------- indicators
@pytest.mark.parametrize("params", [{"period": float("inf")}, {"period": float("nan")}, {"period": "inf"}])
def test_int_param_rejects_non_finite(params):
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    with pytest.raises(ValueError):
        compute_indicator(df, "sma", params)


# -------------------------------------------------------------------- API
@pytest.fixture(scope="module")
def client() -> TestClient:
    main.store.client = FakeClient(NOW)
    return TestClient(main.app)


def test_frontend_files_are_revalidated(client: TestClient):
    """Module scripts must not be served from heuristic browser cache after an update."""
    for path in ("/", "/static/js/app.js", "/static/js/chart.js"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path
    assert "cache-control" not in client.get("/api/intervals").headers


def test_api_rejects_inf_balance_and_amount(client: TestClient):
    r = client.post("/api/sessions", json={"symbol": "BTCUSDT", "start": iso(LISTING + 50 * H), "start_balance": 1e999})
    assert r.status_code == 422
    r = client.post("/api/sessions", json={"symbol": "BTCUSDT", "start": "1969-06-01"})
    assert r.status_code == 422
    r = client.post("/api/sessions", json={"symbol": "BTCUSDT", "start": iso(LISTING + 50 * H), "end": iso(LISTING + 250 * H)})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "ready"
    assert client.post(f"/api/sessions/{sid}/buy", json={"amount": 1e999}).status_code == 422
    assert client.post("/api/compute", json={"symbol": "BTCUSDT", "interval": "1h", "indicators": [{"key": "sma", "params": {"period": 1e999}}]}).status_code == 422

    # A step carrying an invalid view must not move the cursor.
    before = client.get(f"/api/sessions/{sid}").json()["cursor_ms"]
    r = client.post(f"/api/sessions/{sid}/step", json={"n": 3, "view": {"indicators": [{"key": "ema", "params": {"period": 0}}]}})
    assert r.status_code == 422
    assert client.get(f"/api/sessions/{sid}").json()["cursor_ms"] == before

    # Download job status is a snapshot, including after completion.
    r = client.post("/api/data/download", json={"symbol": "BTCUSDT", "interval": "1h", "start": iso(LISTING)})
    assert r.status_code == 202
    job = client.get(f"/api/data/jobs/{r.json()['id']}").json()
    assert job["status"] == "done"
