"""Session endpoints through FastAPI's TestClient, with the fake exchange wired into the app."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.fakes import H, LISTING, FakeClient

NOW = LISTING + 400 * H + 1


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


@pytest.fixture(scope="module")
def client() -> TestClient:
    main.store.client = FakeClient(NOW)  # no network during tests
    return TestClient(main.app)


@pytest.fixture(scope="module")
def session(client: TestClient) -> dict:
    r = client.post(
        "/api/sessions",
        json={"symbol": "BTCUSDT", "start": iso(LISTING + 50 * H), "end": iso(LISTING + 250 * H), "base_interval": "1h"},
    )
    assert r.status_code == 201, r.text
    s = r.json()
    # TestClient runs background tasks before returning, so the session is prepared already.
    st = client.get(f"/api/sessions/{s['id']}").json()
    assert st["status"] == "ready", st
    return st


def test_session_lifecycle(client: TestClient, session: dict):
    sid = session["id"]
    assert session["cursor_ms"] == LISTING + 50 * H and session["price"] is not None
    assert session["account"]["cash"] == 10_000 and session["buckets"] == []
    assert [x["id"] for x in client.get("/api/sessions").json()] == [sid]
    # LISTING + 50 h: two closed days before the start, so the API carries the warm-up warning.
    assert session["start_note"].startswith("BTCUSDT has only 2 daily candles before the start")
    assert client.get("/api/sessions").json()[0]["start_note"] == session["start_note"]

    r = client.post(f"/api/sessions/{sid}/step", json={"interval": "4h"})
    assert r.status_code == 200 and r.json()["cursor_ms"] == LISTING + 51 * H

    r = client.post(f"/api/sessions/{sid}/buy", json={"amount": 1500, "interval": "4h"})
    assert r.status_code == 200, r.text
    b = r.json()["bucket"]
    assert b["buy_rsi_interval"] == "4h" and (b["buy_rsi"] is None or 0 <= b["buy_rsi"] <= 100)
    assert client.post(f"/api/sessions/{sid}/buy", json={"amount": 10, "interval": "1m"}).status_code == 422
    assert b["seq"] == 1 and r.json()["state"]["account"]["cash"] == 8500

    r = client.post(f"/api/sessions/{sid}/step", json={"n": 24})
    assert r.json()["cursor_ms"] == LISTING + 75 * H

    r = client.post(
        f"/api/sessions/{sid}/view",
        json={"interval": "4h", "limit": 30, "indicators": [{"key": "bbands"}], "strategy": {"key": "rsi_reversal"}},
    )
    assert r.status_code == 200, r.text
    v = r.json()
    # Hour 75 completes the 4h candle [72h, 76h): 19 closed 4h candles, no partial one.
    assert v["interval"] == "4h" and v["cursor_ms"] == LISTING + 75 * H and v["count"] == 19
    assert v["partial_last"] is False and v["time"][-1] * 1000 == LISTING + 72 * H
    assert set(v["indicators"][0]["outputs"]) == {"upper", "middle", "lower"}
    assert len(v["strategy"]["state"]) == 19

    r = client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": b["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["bucket"]["status"] == "closed"
    assert client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": b["id"]}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": 424242}).status_code == 404

    j = client.get(f"/api/sessions/{sid}/trades").json()
    assert [t["side"] for t in j["trades"]] == ["buy", "sell"]

    r = client.post(f"/api/sessions/{sid}/reset")
    assert r.json()["cursor_ms"] == LISTING + 50 * H and r.json()["account"]["cash"] == 10_000

    # One request per replay tick: step + view in the same response.
    r = client.post(
        f"/api/sessions/{sid}/step",
        json={"n": 2, "view": {"interval": "1h", "limit": 5, "indicators": [{"key": "sma", "params": {"period": 3}}]}},
    )
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["cursor_ms"] == LISTING + 52 * H
    assert st["view"]["time"][-1] * 1000 == LISTING + 52 * H and st["view"]["count"] == 5
    assert st["view"]["indicators"][0]["outputs"]["sma"][-1] is not None


def test_journal_endpoints(client: TestClient, session: dict):
    sid = session["id"]
    bid = client.post(f"/api/sessions/{sid}/buy", json={"amount": 100}).json()["bucket"]["id"]
    client.post(f"/api/sessions/{sid}/step", json={"n": 2})
    assert client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": bid}).status_code == 200
    rows = [r for r in client.get(f"/api/sessions/{sid}/snapshots").json() if r["bucket_id"] == bid]
    assert [r["side"] for r in rows] == ["buy", "sell"]
    png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    r = client.post(f"/api/sessions/{sid}/snapshots/{bid}/buy/image", json={"image": png, "extra": {"strategy": "none"}})
    assert r.status_code == 200 and r.json()["has_image"] and r.json()["readings"]["strategy"] == "none"
    assert client.post(f"/api/sessions/{sid}/snapshots/{bid}/buy/image", json={"image": "data:image/png;base64,xx"}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/snapshots/{bid}/hold/image", json={"image": png}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/snapshots/424242/buy/image", json={"image": png}).status_code == 404
    img = client.get(f"/api/sessions/{sid}/snapshots/{rows[0]['id']}/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get(f"/api/sessions/{sid}/snapshots/{rows[1]['id']}/image").status_code == 404
    c = client.get(f"/api/sessions/{sid}/snapshots.csv")
    assert c.status_code == 200 and c.headers["content-type"].startswith("text/csv") and len(c.text.splitlines()) >= 3


def test_context_endpoint(client: TestClient, session: dict):
    sid = session["id"]
    r = client.post(f"/api/sessions/{sid}/context", json={})
    assert r.status_code == 200 and r.json()["symbol"] == "BTCUSDT" and r.json()["interval"] == "1D" and r.json()["years"] == 2
    assert set(r.json()["levels"]) == {"resistance", "support", "swings"}
    assert client.post(f"/api/sessions/{sid}/context", json={"years": 9}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/context", json={"tol_pct": 0}).status_code == 422
    assert client.post("/api/sessions/nope/context", json={}).status_code == 404


def test_buy_whole_coins_endpoint(client: TestClient, session: dict):
    sid = session["id"]
    r = client.post(f"/api/sessions/{sid}/buy", json={"amount": 250, "whole": True})
    assert r.status_code == 200
    b = r.json()["bucket"]
    assert b["qty"] == int(b["qty"]) >= 1 and b["cost"] == pytest.approx(b["qty"] * b["entry_price"] / 0.999)
    client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": b["id"]})
    r = client.post(f"/api/sessions/{sid}/buy", json={"amount": 1, "coins": 2})
    assert r.status_code == 200 and r.json()["bucket"]["qty"] == 2.0
    client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": r.json()['bucket']['id']})
    assert client.post(f"/api/sessions/{sid}/buy", json={"amount": 1, "coins": 0}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/buy", json={"amount": 1, "coins": 10**9}).status_code == 422


def test_reasons_endpoints(client: TestClient, session: dict):
    sid = session["id"]
    lists = client.get("/api/reasons").json()
    assert lists["version"] == "v1" and lists["min_group"] == 10
    assert [r["key"] for r in lists["buy"]][:2] == ["at_support", "rsi_low"] and len(lists["buy"]) == 11 and len(lists["sell"]) == 11
    assert lists["buy"][-1] == {"key": "other", "label": "Other"} and lists["sell"][4]["label"] == "Trim"
    r = client.post(f"/api/sessions/{sid}/buy", json={"amount": 100, "reasons": ["nope"]})
    assert r.status_code == 422 and "unknown buy reason" in r.text
    assert client.post(f"/api/sessions/{sid}/buy", json={"amount": 100, "reasons": "at_support"}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/buy", json={"amount": 100, "note": "n" * 501}).status_code == 422
    b = client.post(f"/api/sessions/{sid}/buy", json={"amount": 100, "reasons": ["breakout"], "note": "clean break"}).json()["bucket"]
    assert client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": b["id"], "reasons": ["at_support"]}).status_code == 422
    r = client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": b["id"], "part": 0.5, "reasons": ["trim"]})
    assert r.status_code == 200, r.text
    rows = {(s["bucket_id"], s["side"]): s["readings"] for s in client.get(f"/api/sessions/{sid}/snapshots").json()}
    assert rows[(b["id"], "buy")]["reasons"] == ["breakout"] and rows[(b["id"], "buy")]["reason_note"] == "clean break"
    assert rows[(r.json()["bucket"]["id"], "sell")]["reasons"] == ["trim"] and rows[(r.json()["bucket"]["id"], "sell")]["checklist"] == "v1"
    st = client.get("/api/stats/reasons").json()
    assert st["checklist"] == "v1" and st["sells"] >= 1 and "BTCUSDT" in st["symbols"]
    assert {x["key"]: x for x in st["sell"]}["trim"]["ticked"]["count"] >= 1
    assert client.get("/api/stats/reasons", params={"symbol": "ethusdt"}).json()["sells"] == 0
    assert client.get("/api/stats/reasons", params={"symbol": "x" * 30}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": b["id"]}).status_code == 200


def test_target_stop_endpoints(client: TestClient, session: dict):
    sid = session["id"]
    assert client.post(f"/api/sessions/{sid}/buy", json={"amount": 100, "tp_pct": 0}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/buy", json={"amount": 100, "sl_pct": 100}).status_code == 422
    b = client.post(f"/api/sessions/{sid}/buy", json={"amount": 100, "tp_pct": 20, "sl_pct": 10}).json()["bucket"]
    assert b["tp_pct"] == 20 and b["sl_price"] == pytest.approx(b["entry_price"] * 0.9) and b["armed_ms"] is None
    r = client.post(f"/api/sessions/{sid}/buckets/{b['id']}/arm", json={"armed": True})
    assert r.status_code == 200, r.text
    assert r.json()["bucket"]["armed_ms"] == r.json()["state"]["cursor_ms"] and r.json()["bucket"]["armed_iso"]
    assert client.post(f"/api/sessions/{sid}/buckets/{b['id']}/arm", json={"armed": "yes"}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/buckets/999999/arm", json={"armed": True}).status_code == 404
    st = client.post(f"/api/sessions/{sid}/step", json={"n": 1}).json()
    assert st["auto_sells"] == []
    plain = client.post(f"/api/sessions/{sid}/buy", json={"amount": 50}).json()["bucket"]
    assert client.post(f"/api/sessions/{sid}/buckets/{plain['id']}/arm", json={"armed": True}).status_code == 422
    for bid in (b["id"], plain["id"]):
        assert client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": bid}).status_code == 200
    assert client.post(f"/api/sessions/{sid}/buckets/{b['id']}/arm", json={"armed": True}).status_code == 409


def test_performance_endpoints(client: TestClient, session: dict):
    sid = session["id"]
    r = client.get(f"/api/sessions/{sid}/performance")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["scope"] == "session" and rep["symbol"] == "BTCUSDT" and rep["start_balance"] == 10_000
    assert set(rep) >= {"summary", "curve", "trades", "months"} and len(rep["curve"]["time"]) == rep["curve"]["points"]
    assert rep["summary"]["trades"] == len(rep["trades"]) and rep["summary"]["equity"] == pytest.approx(client.get(f"/api/sessions/{sid}").json()["account"]["equity"])
    assert client.get("/api/sessions/nope/performance").status_code == 404
    allrep = client.get("/api/stats/performance").json()
    assert allrep["scope"] == "all" and allrep["sessions"] >= 1 and allrep["summary"]["trades"] >= rep["summary"]["trades"]
    assert allrep["curve"]["kind"] == "cumulative" and allrep["curve"]["value"][0] == 0


def test_sell_in_parts_endpoint(client: TestClient, session: dict):
    sid = session["id"]
    b = client.post(f"/api/sessions/{sid}/buy", json={"amount": 400}).json()["bucket"]
    r = client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": b["id"], "part": 0.5})
    assert r.status_code == 200, r.text
    part = r.json()["bucket"]
    assert part["part"] == 1 and part["part_pct"] == 50 and part["qty"] == pytest.approx(b["qty"] / 2) and part["id"] != b["id"]
    mine = [x for x in r.json()["state"]["buckets"] if x["id"] == b["id"]][0]
    assert mine["parts_sold"] == 1 and mine["qty"] == pytest.approx(b["qty"] / 2)
    assert client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": b["id"], "part": 0}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": b["id"], "part": 1.01}).status_code == 422
    r = client.post(f"/api/sessions/{sid}/sell", json={"bucket_id": b["id"], "whole": True})
    assert r.status_code == 200 and r.json()["bucket"]["id"] == b["id"] and r.json()["bucket"]["part"] == 2


def test_fib_endpoints(client: TestClient, session: dict):
    sid = session["id"]
    st = client.get(f"/api/sessions/{sid}").json()
    body = {"interval": "1h", "t1_ms": LISTING + 50 * H, "p1": 100.0, "t2_ms": st["cursor_ms"], "p2": 120.0}
    r = client.post(f"/api/sessions/{sid}/fibs", json=body)
    assert r.status_code == 201, r.text
    fib = r.json()
    assert fib["direction"] == "up" and len(fib["levels"]) == 7
    assert client.get(f"/api/sessions/{sid}/fibs").json()[0]["id"] == fib["id"]
    assert client.post(f"/api/sessions/{sid}/fibs", json={**body, "t2_ms": st["cursor_ms"] + H}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/fibs", json={**body, "p2": 1e999}).status_code == 422
    v = client.post(f"/api/sessions/{sid}/view", json={"interval": "1h", "limit": 20, "auto_fib": 10}).json()
    assert v["auto_fib"]["bars"] == 10 and len(v["auto_fib"]["levels"]) == 7
    assert client.post(f"/api/sessions/{sid}/view", json={"interval": "1h", "limit": 20, "auto_fib": 1}).status_code == 422
    v = client.post(f"/api/sessions/{sid}/view", json={"interval": "1h", "limit": 20, "fvg_min_pct": 0.1}).json()
    assert isinstance(v["fvg"], list)
    assert client.post(f"/api/sessions/{sid}/view", json={"interval": "1h", "limit": 20, "fvg_min_pct": 50}).status_code == 422
    assert client.delete(f"/api/sessions/{sid}/fibs/{fib['id']}").status_code == 204
    assert client.delete(f"/api/sessions/{sid}/fibs/{fib['id']}").status_code == 404
    assert client.get(f"/api/sessions/{sid}/fibs").json() == []


def test_frontend_is_served(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200 and "<title>Replay Trading</title>" in r.text
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/static/vendor/lightweight-charts.standalone.production.js").status_code == 200


def test_session_validation(client: TestClient, session: dict):
    sid = session["id"]
    assert client.post(f"/api/sessions/{sid}/buy", json={"amount": 1e9}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/buy", json={"amount": -5}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/step", json={"interval": "5m"}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/view", json={"indicators": [{"key": "nope"}]}).status_code == 422
    assert client.get("/api/sessions/nope").status_code == 404
    assert client.post("/api/sessions", json={"symbol": "FOO", "start": "2024-01-01"}).status_code == 422
    assert client.post("/api/sessions", json={"symbol": "BTCUSDT", "start": "not-a-date"}).status_code == 422
    r = client.post("/api/sessions", json={"symbol": "BTCUSDT", "start": iso(LISTING + 50 * H), "end": iso(LISTING + 10 * H)})
    assert r.status_code == 422


def test_session_without_data_reports_error(client: TestClient):
    r = client.post("/api/sessions", json={"symbol": "ETHUSDT", "start": iso(LISTING - 100 * H), "end": iso(LISTING - 50 * H)})
    assert r.status_code == 201
    st = client.get(f"/api/sessions/{r.json()['id']}").json()
    assert st["status"] == "error" and "no 1h candles" in st["error"]
    assert client.post(f"/api/sessions/{st['id']}/step", json={}).status_code == 409
    assert client.delete(f"/api/sessions/{st['id']}").status_code == 204
    assert client.delete(f"/api/sessions/{st['id']}").status_code == 404


def test_view_carries_the_highs_and_lows(client: TestClient, session: dict):
    r = client.post(f"/api/sessions/{session['id']}/view", json={"interval": "1h", "limit": 3})
    assert r.status_code == 200, r.text
    ex = r.json()["gauges"]["extremes"]
    assert {"all", "1y", "3m", "complete", "interval"} <= set(ex) and ex["all"]["from_ms"] == LISTING
    assert ex["all"]["vs_high_pct"] <= 0 <= ex["all"]["vs_low_pct"] and 0 <= ex["3m"]["position"] <= 1


def test_view_levels_and_tolerance(client: TestClient, session: dict):
    r = client.post(f"/api/sessions/{session['id']}/view", json={"interval": "1h", "limit": 3, "sr_tol": 2.5})
    assert r.status_code == 200, r.text
    lv = r.json()["gauges"]["levels"]
    assert lv["tol_pct"] == 2.5 and lv["min_touches"] == 3 and lv["max_visit_bars"] == 5 and {"resistance", "support", "bars"} <= set(lv)
    assert set(r.json()["gauges"]["extremes"]) >= {"all", "1y", "6m", "3m"}
    assert client.post(f"/api/sessions/{session['id']}/view", json={"interval": "1h", "limit": 3, "sr_tol": 0}).status_code == 422
    assert client.post(f"/api/sessions/{session['id']}/buy", json={"amount": 100, "sr_tol": 11}).status_code == 422
