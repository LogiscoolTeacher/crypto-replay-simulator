"""Day-trading endpoints through FastAPI's TestClient, with the 15-minute fake exchange wired in."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.fakes import DAY, H, LISTING, FineFakeClient

START = LISTING + 300 * DAY          # 2018-05-10 00:00 UTC
END = START + 6 * DAY
NOW = START + 10 * DAY + 1
FIRST_CURSOR = START                 # the first candle of 2018-05-10 (the day starts at 00:00 UTC)
SYMBOL = "SOLUSDT"   # a coin no other test module downloads, so their stored candles stay untouched
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


@pytest.fixture(scope="module")
def client() -> TestClient:
    main.store.client = FineFakeClient(NOW)  # no network during tests
    return TestClient(main.app)


@pytest.fixture(scope="module")
def session(client: TestClient):
    r = client.post("/api/day/sessions", json={"symbol": SYMBOL, "start": iso(START), "end": iso(END), "start_balance": 100_000})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["mode"] == "day" and s["status"] in ("preparing", "ready")
    st = client.get(f"/api/day/sessions/{s['id']}").json()   # TestClient ran the background prepare already
    assert st["status"] == "ready", st
    yield st
    client.delete(f"/api/sessions/{s['id']}")   # leave nothing behind for the other API test modules


def test_day_session_lifecycle(client: TestClient, session: dict):
    sid = session["id"]
    base = f"/api/day/sessions/{sid}"
    st = session
    assert st["base_interval"] == "15m" and st["cursor_ms"] == FIRST_CURSOR and st["fee_rate"] == 0.001
    assert st["day"]["date"] == "2018-05-10" and st["day"]["risk_amount"] == 100 and "hh_hl" not in st["day"]["check"]
    assert st["day"]["entries_allowed"] is True and st["prepare_progress"] is None   # the trend check never blocks
    assert st["name"] == f"Day {SYMBOL} from 2018-05-10"
    # the shared listing shows the mode; the bucket controls refuse the session; the swing state still answers
    assert [x["mode"] for x in client.get("/api/sessions").json() if x["id"] == sid] == ["day"]
    assert client.post(f"/api/sessions/{sid}/step", json={}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/buy", json={"amount": 100}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/reset").status_code == 422
    assert client.get(f"/api/sessions/{sid}").json()["mode"] == "day"
    assert client.post(f"{base}/prepare").json()["status"] == "ready"

    # the daily "higher highs and higher lows" answer is gone: no /check route any more
    assert client.post(f"{base}/check", json={"hh_hl": True}).status_code in (404, 405)
    st = client.get(f"/api/day/sessions/{sid}").json()
    assert st["day"]["check"]["ema_above"] is True and st["day"]["entries_allowed"] is True
    price = st["price"]

    # zones
    assert client.post(f"{base}/zones", json={"interval": "15m", "p1": 1, "p2": 2}).status_code == 422
    assert client.post(f"{base}/zones", json={"interval": "1h", "p1": 0, "p2": 2}).status_code == 422
    r = client.post(f"{base}/zones", json={"interval": "1h", "p1": price - 3.0, "p2": price + 0.5})
    assert r.status_code == 201, r.text
    z = r.json()
    assert z["upper"] == price + 0.5 and z["lower"] == price - 3.0 and z["interval"] == "1h"
    r = client.put(f"{base}/zones/{z['id']}", json={"p1": price + 0.6, "p2": price - 3.0})
    assert r.status_code == 200 and r.json()["upper"] == price + 0.6
    assert client.get(f"{base}/zones").json() == [r.json()]
    assert client.put(f"{base}/zones/424242", json={"p1": 1, "p2": 2}).status_code == 404

    # plan
    assert client.post(f"{base}/plan", json={}).status_code == 422          # no zone chosen
    r = client.post(f"{base}/plan", json={"zone_id": z["id"]})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["entry_price"] == price and p["can_buy"] is False and p["take_profit"] is None
    assert p["stop_suggested"] == pytest.approx(price - 1.2) and p["stop_used"] == p["stop_suggested"]   # middle of the zone
    assert p["stop_atr"] == pytest.approx((price - 3.0) - 0.5 * p["atr"])
    assert p["coins"] == pytest.approx(100.0 / (price - p["stop_used"])) and p["capped"] is False
    tp = price + 2.0 * (price - p["stop_used"])
    p = client.post(f"{base}/plan", json={"zone_id": z["id"], "take_profit": tp}).json()
    assert p["rr"] == pytest.approx(2.0) and p["warnings"] == [] and p["can_buy"] is True
    low = client.post(f"{base}/plan", json={"zone_id": z["id"], "take_profit": price + 1.0}).json()
    assert low["can_buy"] is True and any("below 2.0" in w for w in low["warnings"])
    assert client.post(f"{base}/plan", json={"zone_id": z["id"], "stop": price + 1}).json()["errors"]
    r = client.post(f"{base}/plan", json={"zone_id": z["id"], "ratio": 2.0})          # target from the ratio
    assert r.status_code == 200 and r.json()["take_profit"] == pytest.approx(tp) and r.json()["rr"] == pytest.approx(2.0)
    assert client.post(f"{base}/plan", json={"zone_id": z["id"], "ratio": 0}).status_code == 422
    assert client.post(f"{base}/plan", json={"zone_id": z["id"], "ratio": 101}).status_code == 422

    # buy
    assert client.post(f"{base}/buy", json={"zone_id": z["id"]}).status_code == 422           # take-profit missing
    assert client.post(f"{base}/buy", json={"zone_id": z["id"], "take_profit": price, "confidence": 5}).status_code == 422
    assert client.post(f"{base}/buy", json={"zone_id": z["id"], "take_profit": tp}).status_code == 422               # confidence missing
    assert client.post(f"{base}/buy", json={"zone_id": z["id"], "take_profit": tp, "confidence": 11}).status_code == 422
    assert client.post(f"{base}/buy", json={"zone_id": z["id"], "take_profit": tp, "confidence": 7.5}).status_code == 422
    r = client.post(f"{base}/buy", json={"zone_id": z["id"], "take_profit": tp, "notes": "via api", "confidence": 7})
    assert r.status_code == 200, r.text
    t = r.json()["trade"]
    assert t["status"] == "open" and t["notes"] == "via api" and t["rr"] == pytest.approx(2.0) and t["confidence"] == 7
    assert r.json()["state"]["day"]["trade"]["id"] == t["id"] and r.json()["state"]["cash"] < 100_000
    assert client.post(f"{base}/buy", json={"zone_id": z["id"], "take_profit": tp, "confidence": 7}).status_code == 409
    assert client.post(f"{base}/skip").status_code == 409

    # move the stop and the target (saved with the trade)
    assert client.patch(f"{base}/trade", json={}).status_code == 422                               # nothing to move
    assert client.patch(f"{base}/trade", json={"stop": -1}).status_code == 422
    assert client.patch(f"{base}/trade", json={"stop": price + 1}).status_code == 422               # above the price
    assert client.patch(f"{base}/trade", json={"take_profit": price - 1}).status_code == 422        # below the price
    r = client.patch(f"{base}/trade", json={"stop": t["stop_used"] + 0.1, "take_profit": tp + 0.5})
    assert r.status_code == 200, r.text
    mv = r.json()["trade"]
    assert mv["stop_used"] == pytest.approx(t["stop_used"] + 0.1) and mv["take_profit"] == pytest.approx(tp + 0.5)
    assert mv["stop_initial"] == pytest.approx(t["stop_used"]) and mv["tp_initial"] == pytest.approx(tp)
    assert [c["field"] for c in mv["changes"]] == ["stop", "take_profit"] and mv["conditions"]["stop_moved"] is True
    assert mv["to_stop_pct"] < 0 < mv["to_tp_pct"] and "price_change_pct" in mv
    assert r.json()["state"]["day"]["trade"]["stop_used"] == pytest.approx(t["stop_used"] + 0.1)

    # step back: not before the entry (nor its stop / target move)
    assert client.post(f"{base}/step-back", json={}).status_code == 409

    # step, with and without a view; a bad view does not move the cursor
    r = client.post(f"{base}/step", json={"n": 2, "view": {"interval": "1h", "limit": 20, "indicators": [{"key": "atr"}]}})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["cursor_ms"] == FIRST_CURSOR + 2 * 900_000 and st["events"] == []
    assert st["view"]["interval"] == "1h" and st["view"]["indicators"][0]["key"] == "atr" and st["day"]["trade"]["status"] == "open"
    assert client.post(f"{base}/step", json={"n": 1, "view": {"indicators": [{"key": "nope"}]}}).status_code == 422
    assert client.get(base).json()["cursor_ms"] == st["cursor_ms"]
    r = client.post(f"{base}/step-back", json={"view": {"interval": "15m", "limit": 10}})
    assert r.status_code == 200, r.text
    assert r.json()["cursor_ms"] == st["cursor_ms"] - 900_000 and r.json()["view"]["interval"] == "15m"
    st = client.post(f"{base}/step", json={"n": 1}).json()

    # early exit
    assert client.post(f"{base}/sell", json={"reason": "other"}).status_code == 422
    assert client.post(f"{base}/sell", json={"reason": "nonsense"}).status_code == 422
    assert client.post(f"{base}/sell", json={"reason": "stalls", "exits": "B"}).status_code == 422
    r = client.post(f"{base}/sell", json={"reason": "stalls", "exits": "A"})   # exit A only: the trade stays open
    assert r.status_code == 200 and r.json()["events"][0]["kind"] == "exit_leg" and r.json()["day"]["open_exits"] == ["C"]
    assert client.post(f"{base}/sell", json={"reason": "stalls", "exits": "A"}).status_code == 409
    r = client.post(f"{base}/sell", json={"reason": "stalls"})                  # "both": closes what is left (C)
    assert r.status_code == 200, r.text
    ev = r.json()["events"][0]
    assert ev["kind"] == "exit" and ev["exit_type"] == "early exit" and r.json()["day"]["phase"] == "exited"
    tid = ev["trade"]["id"]
    assert ev["trade"]["exit_ms"] == st["cursor_ms"] and ev["trade"]["whatif_n"] > 0
    assert r.json()["cursor_ms"] == st["cursor_ms"] and ev["choice"] is True and r.json()["day"]["exit_choice"] is True
    assert r.json()["day"]["can_step_back"] is False                                               # the exit is the floor
    assert client.post(f"{base}/step-back", json={}).status_code == 409
    # end the day: the jump to 24:00 (= 00:00 of the next day), where the review is asked
    r = client.post(f"{base}/end-day")
    assert r.status_code == 200, r.text
    assert r.json()["events"][0]["kind"] == "jump" and r.json()["day"]["local_time"] == "2018-05-11 00:00"
    assert r.json()["day"]["date"] == "2018-05-11" and r.json()["day"]["phase"] == "trading"
    assert r.json()["day"]["review_pending"]["id"] == tid and r.json()["day"]["can_continue"] is False
    assert client.post(f"{base}/end-day").status_code == 409
    assert client.post(f"{base}/sell", json={"reason": "stalls"}).status_code == 409
    # the review: required before anything moves, all four marks, true / false only
    assert client.post(f"{base}/continue").status_code == 409
    assert client.post(f"{base}/skip").status_code == 409
    assert client.post(f"{base}/step", json={"n": 1}).status_code == 409
    assert client.post(f"{base}/trades/{tid}/review", json={"stop_tight": True}).status_code == 422
    assert client.post(f"{base}/trades/{tid}/review", json={"stop_tight": False, "over_target": False, "stop_saved": False}).status_code == 422
    assert client.post(f"{base}/trades/{tid}/review", json={"stop_tight": 1, "over_target": False, "stop_saved": False, "stop_moved_up": False}).status_code == 422
    assert client.post(f"{base}/trades/424242/review", json={"stop_tight": False, "over_target": False, "stop_saved": False, "stop_moved_up": False}).status_code == 404
    r = client.post(f"{base}/trades/{tid}/review", json={"stop_tight": False, "over_target": True, "stop_saved": False, "stop_moved_up": False})
    assert r.status_code == 200, r.text
    assert r.json()["trade"]["review"]["over_target"] is True and r.json()["trade"]["review"]["reviewed"] is True
    assert r.json()["state"]["day"]["review_pending"] is None and r.json()["state"]["day"]["can_skip"] is True
    assert client.patch(f"{base}/trade", json={"stop": 1.0}).status_code == 409                    # closed: nothing to move

    # the picture at the exit
    r = client.post(f"{base}/trades/{tid}/image", json={"image": PNG})
    assert r.status_code == 200 and r.json()["has_image"] is True
    img = client.get(f"{base}/trades/{tid}/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.post(f"{base}/trades/{tid}/image", json={"image": "data:image/png;base64,xx"}).status_code == 422
    assert client.get(f"{base}/trades/424242/image").status_code == 404
    rows = client.get(f"{base}/trades").json()["trades"]
    assert len(rows) == 1 and rows[0]["exit_reason_label"] == "Price stalls at resistance" and rows[0]["has_image"]

    # continue: nothing left to continue (End the day already started the next day); skip -> the day
    # after; reset -> back to the start
    r = client.post(f"{base}/continue")
    assert r.status_code == 200 and r.json()["events"] == [] and r.json()["day"]["date"] == "2018-05-11"
    r = client.post(f"{base}/skip")
    assert r.status_code == 200 and r.json()["day"]["date"] == "2018-05-12"
    r = client.post(f"{base}/reset")
    assert r.status_code == 200 and r.json()["cursor_ms"] == FIRST_CURSOR and r.json()["cash"] == 100_000
    assert client.get(f"{base}/trades").json()["trades"] == [] and client.get(f"{base}/zones").json() == []
    assert client.delete(f"{base}/zones/{z['id']}").status_code == 404


def test_day_endpoints_reject_other_sessions(client: TestClient, session: dict):
    assert client.get("/api/day/sessions/nope").status_code == 404
    r = client.post("/api/sessions", json={"symbol": SYMBOL, "start": iso(START), "end": iso(END), "base_interval": "1h"})
    assert r.status_code == 201
    sw = r.json()["id"]
    try:
        assert client.get(f"/api/day/sessions/{sw}").status_code == 422
        assert client.post(f"/api/day/sessions/{sw}/step", json={}).status_code == 422
        assert client.get(f"/api/day/sessions/{sw}/zones").status_code == 422
    finally:
        assert client.delete(f"/api/sessions/{sw}").status_code == 204
    assert client.post("/api/day/sessions", json={"symbol": "NOPE", "start": iso(START)}).status_code == 422
    assert client.post("/api/day/sessions", json={"symbol": SYMBOL, "start": "yesterday"}).status_code == 422
    assert client.post("/api/day/sessions", json={"symbol": SYMBOL, "start": iso(START), "start_balance": 0}).status_code == 422


def test_daily_journal_routes(client: TestClient, session: dict):
    sid = session["id"]
    base = f"/api/day/sessions/{sid}"
    st = client.get(base).json()
    today = st["day"]["date"]
    assert st["day"]["leaving_next"] is False
    r = client.put(f"{base}/days/{today}/notes", json={"notes": "first day"})
    assert r.status_code == 200 and r.json()["notes"] == "first day" and r.json()["outcome"] == "current"
    assert client.put(f"{base}/days/2099-01-01/notes", json={"notes": "x"}).status_code == 404
    assert client.put(f"{base}/days/nope/notes", json={"notes": "x"}).status_code == 422
    assert client.get(f"{base}/days/{today}/image").status_code == 404
    r = client.post(f"{base}/days/{today}/image", json={"image": PNG})
    assert r.status_code == 200 and r.json()["has_image"] is True
    img = client.get(f"{base}/days/{today}/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    r = client.post(f"{base}/skip")
    assert r.status_code == 200
    days = client.get(f"{base}/days").json()["days"]
    assert len(days) == 2 and days[0]["outcome"] == "skipped" and days[0]["notes"] == "first day" and days[0]["has_image"] is True
    csv = client.get(f"{base}/days.csv")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv") and csv.text.splitlines()[1].startswith(today)
    assert client.get("/api/day/sessions/nope/days").status_code == 404
    client.post(f"{base}/reset")


def test_view_rsi_panes(client: TestClient, session: dict):
    """The day chart asks for the 15m and 1h RSI as extra panes through the shared view route."""
    sid = session["id"]
    r = client.post(f"/api/sessions/{sid}/view", json={"interval": "15m", "limit": 40, "rsi_panes": ["15m", "1h"]})
    assert r.status_code == 200, r.text
    v = r.json()
    assert [p["interval"] for p in v["rsi_panes"]] == ["15m", "1h"]
    for p in v["rsi_panes"]:
        assert p["period"] == 14 and len(p["values"]) == v["count"] == 40
        assert all(x is None or 0 <= x <= 100 for x in p["values"]) and p["values"][-1] is not None
    # the 1h pane changes at most every fourth 15-minute bar
    hourly = v["rsi_panes"][1]["values"]
    changes = sum(1 for a, b in zip(hourly, hourly[1:]) if a != b)
    assert changes <= 11
    assert client.post(f"/api/sessions/{sid}/view", json={"interval": "15m", "limit": 5, "rsi_panes": ["5m"]}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/view", json={"interval": "15m", "limit": 5, "rsi_panes": ["15m", "1h", "4h", "1D", "1W"]}).status_code == 422
    # the step carries the panes with its view
    r = client.post(f"/api/day/sessions/{sid}/step", json={"n": 1, "view": {"interval": "15m", "limit": 5, "rsi_panes": ["1h"]}})
    assert r.status_code == 200, r.text
    assert [p["interval"] for p in r.json()["view"]["rsi_panes"]] == ["1h"]


def test_goto_route(client: TestClient, session: dict):
    base = f"/api/day/sessions/{session['id']}"
    client.post(f"{base}/reset")
    assert client.post(f"{base}/goto", json={"date": "May 12"}).status_code == 422
    assert client.post(f"{base}/goto", json={"date": "2018-05-10"}).status_code == 422          # not after the current day
    r = client.post(f"{base}/goto", json={"date": "2018-05-12"})
    assert r.status_code == 200, r.text
    assert r.json()["events"][0]["kind"] == "jump" and r.json()["day"]["date"] == "2018-05-12"
    client.post(f"{base}/reset")


def test_auto_trades_route(client: TestClient, session: dict):
    base = f"/api/day/sessions/{session['id']}"
    client.post(f"{base}/reset")
    a0 = client.get(f"{base}/auto-trades").json()
    assert a0["trades"] == [] and a0["summary"]["count"] == 0
    price = client.get(base).json()["price"]
    z = client.post(f"{base}/zones", json={"interval": "1h", "p1": price + 0.5, "p2": price - 0.5}).json()
    r = client.post(f"{base}/buy", json={"zone_id": z["id"], "take_profit": price + 50.0, "stop": price - 50.0, "confidence": 5})
    assert r.status_code == 200, r.text
    a = client.get(f"{base}/auto-trades").json()
    assert a["summary"]["count"] == 1 and a["trades"][0]["status"] == "open" and a["trades"][0]["manual"]["status"] == "open"
    assert r.json()["state"]["day"]["auto"]["seq"] == a["trades"][0]["seq"]
    client.post(f"{base}/reset")


def test_next_ema_day_route(client: TestClient, session: dict):
    base = f"/api/day/sessions/{session['id']}"
    client.post(f"{base}/reset")
    r = client.post(f"{base}/next-ema-day")
    assert r.status_code == 200, r.text
    assert r.json()["events"][0]["kind"] == "jump" and r.json()["day"]["date"] == "2018-05-11"
    client.post(f"{base}/reset")


def test_next_signal_routes(client: TestClient, session: dict):
    base = f"/api/day/sessions/{session['id']}"
    strategies = client.get("/api/day/strategies").json()
    assert [x["key"] for x in strategies] == ["rline_retest"] and strategies[0]["interval"] == "1h" and strategies[0]["params"]
    client.post(f"{base}/reset")
    assert client.post(f"{base}/next-signal", json={"strategy": "nope"}).status_code == 422
    assert client.post(f"{base}/next-signal", json={"params": {"retest_max": "x"}}).status_code == 422
    r = client.post(f"{base}/next-signal", json={})   # the fake market's tiny waves give no RLine
    assert r.status_code == 409 and "No later RLine_Retest signal" in r.text
    client.post(f"{base}/reset")


def test_conditions_routes(client: TestClient, session: dict):
    sid = session["id"]
    r = client.get(f"/api/day/sessions/{sid}/trades")
    assert r.status_code == 200 and all("conditions" in t and "ema_above" in t for t in r.json()["trades"])
    r = client.get(f"/api/day/sessions/{sid}/conditions")
    assert r.status_code == 200, r.text
    ev = r.json()
    assert ev["scope"] == "session" and len(ev["conditions"]) == 14 and {"with", "without", "unknown", "key", "label"} <= set(ev["conditions"][0])
    assert client.get(f"/api/day/sessions/{sid}/conditions", params={"scope": "all"}).json()["scope"] == "all"
    assert client.get(f"/api/day/sessions/{sid}/conditions", params={"scope": "week"}).status_code == 422
    r = client.get(f"/api/day/sessions/{sid}/conditions.csv", params={"scope": "all"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and r.text.startswith("condition,label,")
    assert client.get("/api/day/sessions/nope/conditions").status_code == 404
