"""API tests with FastAPI's TestClient against a database seeded by the fake exchange."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config
from app.data.candle_store import CandleStore
from app.main import app
from tests.fakes import H, LISTING, FakeClient

N_BARS = 300


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Seed the same SQLite file the app uses, through a store wired to the fake exchange.
    seed = CandleStore(config.DB_PATH, FakeClient(LISTING + N_BARS * H + 1))
    seed.ensure_range("BTCUSDT", "1h", LISTING)
    return TestClient(app)


def iso(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def test_catalogues(client: TestClient):
    assert [c["symbol"] for c in client.get("/api/coins").json()][:2] == ["BTCUSDT", "ETHUSDT"]
    assert [i["name"] for i in client.get("/api/intervals").json()] == ["1m", "5m", "15m", "1h", "4h", "1D"]
    inds = client.get("/api/indicators").json()
    assert {i["key"] for i in inds} == {"sma", "ema", "rsi", "macd", "bbands", "atr", "vdelta"}
    strats = client.get("/api/strategies").json()
    assert {s["key"] for s in strats} == {"rsi_reversal", "ema_cross", "macd_cross", "bb_bounce"}


def test_candles_endpoint(client: TestClient):
    r = client.get("/api/candles", params={"symbol": "BTCUSDT", "interval": "1h", "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 5
    assert body["candles"][-1]["time"] == (LISTING + (N_BARS - 1) * H) // 1000


def test_compute_defaults(client: TestClient):
    r = client.post(
        "/api/compute",
        json={
            "symbol": "BTCUSDT",
            "interval": "1h",
            "limit": 50,
            "indicators": [{"key": "ema", "params": {"period": 20}}, {"key": "rsi"}],
            "strategy": {"key": "ema_cross", "params": {"fast": 5, "slow": 20}},
        },
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["count"] == 50 and len(b["time"]) == 50 and len(b["close"]) == 50
    assert b["time"][-1] == (LISTING + (N_BARS - 1) * H) // 1000
    assert [i["key"] for i in b["indicators"]] == ["ema", "rsi"]
    assert b["indicators"][0]["params"] == {"period": 20}
    assert b["indicators"][1]["params"] == {"period": 14}
    ema = b["indicators"][0]["outputs"]["ema"]
    assert len(ema) == 50 and all(v is not None for v in ema)  # 300 bars of history: fully warmed up
    s = b["strategy"]
    assert s["key"] == "ema_cross" and s["params"] == {"fast": 5, "slow": 20}
    assert len(s["state"]) == 50 and set(s["state"]) <= {-1, 0, 1}
    assert len(s["enter"]) == 50 and isinstance(s["enter"][0], bool)


def test_compute_respects_end_cursor_and_anchor(client: TestClient):
    end = LISTING + 99 * H
    r = client.post(
        "/api/compute",
        json={"symbol": "BTCUSDT", "interval": "1h", "end": iso(end), "limit": 10, "indicators": [{"key": "sma", "params": {"period": 5}}]},
    )
    b = r.json()
    assert b["time"][-1] == end // 1000 and b["count"] == 10
    # Anchoring the start at the cursor's bar leaves only one bar: SMA(5) is NaN -> null.
    r = client.post(
        "/api/compute",
        json={"symbol": "BTCUSDT", "interval": "1h", "start": iso(end), "end": iso(end), "indicators": [{"key": "sma", "params": {"period": 5}}]},
    )
    b = r.json()
    assert b["count"] == 1 and b["indicators"][0]["outputs"]["sma"] == [None]


def test_compute_empty_window(client: TestClient):
    r = client.post("/api/compute", json={"symbol": "ETHUSDT", "interval": "1h", "indicators": [{"key": "rsi"}]})
    assert r.status_code == 200
    b = r.json()
    assert b["count"] == 0 and b["indicators"][0]["outputs"]["rsi"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"symbol": "FOO", "interval": "1h"},
        {"symbol": "BTCUSDT", "interval": "2h"},
        {"symbol": "BTCUSDT", "interval": "1h", "indicators": [{"key": "nope"}]},
        {"symbol": "BTCUSDT", "interval": "1h", "indicators": [{"key": "ema", "params": {"period": 0}}]},
        {"symbol": "BTCUSDT", "interval": "1h", "strategy": {"key": "ema_cross", "params": {"fast": 50, "slow": 20}}},
        {"symbol": "BTCUSDT", "interval": "1h", "limit": 0},
        {"symbol": "BTCUSDT", "interval": "1h", "end": "yesterday"},
    ],
)
def test_compute_rejects_bad_input(client: TestClient, payload):
    r = client.post("/api/compute", json=payload)
    assert r.status_code == 422, r.text
