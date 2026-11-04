"""Autorun: the automatic RLine_Retest run (confirmation candles, fee filter, signal log, what-ifs,
pictures) against the 15-minute fake exchange with scripted signals and candles."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main
from app.replay.autorun import (
    Autorun,
    AutorunRules,
    confirmation,
    is_bullish_engulfing,
    is_hammer,
    r_results,
    stats,
    walk,
)
from app.replay.engine import Conflict, Invalid
from tests.fakes import DAY, H, LISTING, Q, quarter_price
from tests.test_day import END, NOW, PNG, START, make

D1, D2, D3, D4 = (START + k * DAY + 6 * H for k in (1, 2, 3, 4))   # 06:00 of days 1-4 (the fake's wave starts there: close = trend)


def P(t: int) -> float:
    """The fake's close of the 15m candle opening at t."""
    return quarter_price(t)


def sig(bar_ms: int, top: float, bottom: float) -> dict:
    return {"bar": 0, "bar_ms": bar_ms, "zone_top": top, "zone_bottom": bottom, "entry_level": top, "near": False,
            "rules": ["A"], "touches": 2, "touch_ms": [], "breakout_ms": bar_ms - 5 * H, "psych": None}


# The scenario of the main test: the candles the signals need.
OVERRIDES = {
    # D1: a hammer two candles after the signal (body 0.2, lower wick 0.6, upper wick 0.05) ...
    D1 + 2 * Q: (P(D1 + Q), P(D1) + 0.35, P(D1) - 0.5, P(D1) + 0.3),
    # ... and a spike to the 2R target six candles later
    D1 + 8 * Q: (P(D1 + 7 * Q), P(D1) + 10.0, P(D1 + 7 * Q) - 0.3, P(D1 + 7 * Q)),
    # D2: a hammer right after the signal, but the zone is thin: the stop is too close for the fees
    D2 + Q: (P(D2), P(D2) + 0.15, P(D2) - 0.3, P(D2) + 0.1),
    # D4: the third candle closes below the zone
    D4 + 3 * Q: (P(D4 + 2 * Q), P(D4 + 2 * Q) + 0.1, P(D4) - 3.5, P(D4) - 3.0),
}


def signals() -> list[dict]:
    return [
        sig(D1, P(D1) + 0.5, P(D1) - 2.0),          # traded: hammer, target
        sig(D1 + H, P(D1) + 0.5, P(D1) - 2.0),      # during that trade
        sig(D2, P(D2) + 0.5, P(D2) - 0.4),          # confirmed, fees too high
        sig(D3, P(D3) + 0.5, P(D3) - 2.0),          # nothing confirms within the window
        sig(D3 + H, P(D3) + 0.5, P(D3) - 2.0),      # inside that window
        sig(D4, P(D4) + 0.5, P(D4) - 2.0),          # zone broken
    ]


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    day = make(tmp_path / "replay.db", OVERRIDES)
    s = day.prepare(day.create_session("BTCUSDT", START, END, start_balance=100_000, name="CTest__01")["id"])
    fake = signals()
    monkeypatch.setattr(day, "_strategy_signals", lambda s, js, p: fake)
    return day, Autorun(day), s["id"], fake


def run_all(day, ar: Autorun, sid: str, pictures: bool = False) -> list[dict]:
    """Advance until done, standing in for the frontend: it takes the day and trade pictures asked for."""
    calls = []
    for _ in range(500):
        st = ar.advance(sid)
        calls.append(st)
        if st["autorun"]["need"] == "day_picture":
            day.attach_day_image(sid, st["day"]["date"], PNG)
        if pictures and st["day"]["picture_due"]:
            day.attach_image(sid, st["day"]["picture_due"]["id"], PNG)
        if st["autorun"]["phase"] == "done":
            return calls
    raise AssertionError("the run did not finish")


# ------------------------------------------------------------- patterns
def test_candle_patterns():
    assert is_hammer((10.0, 10.25, 9.0, 10.2), entry_level=9.5)               # body 0.2, lower 1.0, upper 0.05
    assert is_hammer((10.2, 10.25, 9.0, 10.0), entry_level=9.5)               # red hammers count too
    assert not is_hammer((10.0, 10.25, 9.0, 10.2), entry_level=8.9)           # the low must reach the zone
    assert not is_hammer((10.0, 10.6, 9.0, 10.2), entry_level=9.5)            # upper wick 0.4 > body
    assert not is_hammer((10.0, 10.25, 9.7, 10.2), entry_level=9.8)           # lower wick 0.3 < 2 x body
    assert not is_hammer((10.0, 10.5, 9.0, 10.0), entry_level=9.5)            # no body
    assert is_bullish_engulfing((10.0, 10.1, 9.4, 9.5), (9.5, 10.2, 9.4, 10.1))
    assert not is_bullish_engulfing((10.0, 10.1, 9.4, 9.5), (9.6, 10.2, 9.4, 10.1))   # opens above the red close
    assert not is_bullish_engulfing((10.0, 10.1, 9.4, 9.5), (9.5, 10.2, 9.4, 9.9))    # closes below the red open
    assert not is_bullish_engulfing((9.5, 10.1, 9.4, 10.0), (9.5, 10.2, 9.4, 10.1))   # the previous one is green
    rules = AutorunRules()
    assert confirmation(None, (10.0, 10.25, 8.0, 8.5), zone_bottom=9.0, entry_level=10.0, rules=rules) == "broken"   # the break wins
    assert confirmation(None, (10.0, 10.25, 9.0, 10.2), 9.0, 9.5, rules) == "hammer"
    assert confirmation((10.0, 10.1, 9.4, 9.5), (9.5, 10.2, 9.4, 10.1), 9.0, 9.5, rules) == "engulfing"
    assert confirmation((10.0, 10.1, 9.4, 9.5), (9.5, 10.2, 9.4, 10.1), 9.0, 9.5, AutorunRules(engulfing=False)) is None


def test_walk_and_r():
    t = np.array([1, 2, 3, 4])
    hi = np.array([10.0, 11.0, 13.0, 10.0])
    lo = np.array([9.0, 9.5, 8.0, 9.0])
    assert walk(t, hi, lo, 0, stop=8.5, target=12.0) == ("stop", 3)            # stop and target in one candle: the stop
    assert walk(t, hi, lo, 0, stop=8.5, target=10.5) == ("target", 2)
    assert walk(t, hi, lo, 3, stop=8.5, target=12.0) == ("open", None)
    gross, net = r_results("target", entry=100.0, stop=99.0, target=102.0, fee_rate=0.001)
    assert gross == pytest.approx(2.0) and net == pytest.approx(2.0 - 0.001 * 202.0)
    assert r_results("open", 100.0, 99.0, 102.0, 0.001) == (None, None)
    st = stats([2.0, -1.0, -1.0, 2.0])
    assert st["count"] == 4 and st["wins"] == 2 and st["win_rate"] == 50.0 and st["total_r"] == 2.0 and st["max_dd_r"] == 2.0


def test_rules_are_validated():
    assert AutorunRules.from_dict({}).params["away_atr"] == 0.75                # the strategy's defaults are filled in
    for bad in ({"nope": 1}, {"confirm_bars": 0}, {"ratio": 0}, {"hammer": False, "engulfing": False},
                {"entry_until": "2023-13-01"}, {"stop_atr_interval": "1m"}, {"params": {"min_gap": 0}}):
        with pytest.raises(Invalid):
            AutorunRules.from_dict(bad)


# -------------------------------------------------------------- the run
def test_autorun_trades_confirmed_signals_and_logs_every_signal(env):
    day, ar, sid, _ = env
    ar.start(sid, {"engulfing": False, "pictures": False})
    with pytest.raises(Conflict):
        ar.start(sid, {})                                    # one run per session
    calls = run_all(day, ar, sid)
    status = calls[-1]["autorun"]
    assert status["done"] == "no more signals before the end of the data"
    rows = ar.signals(sid)
    assert [r["outcome"] for r in rows] == ["traded", "trade_open", "fees_too_high", "no_confirmation", "waiting", "zone_broken"]
    assert [r["signal_ms"] for r in rows] == [D1, D1 + H, D2, D3, D3 + H, D4]

    # the trade: entry at the hammer's close, stop below the zone by 0.25 x ATR 1h, target 2R, risk 100
    tr = day.trades(sid)["trades"][0]
    log = rows[0]
    atr = ar._atr(day._ready(sid), "1h", D1 + 3 * Q)
    assert log["pattern"] == "hammer" and log["confirm_ms"] == D1 + 2 * Q and log["trade_id"] == tr["id"]
    assert tr["entry_ms"] == D1 + 2 * Q and tr["entry_price"] == pytest.approx(P(D1) + 0.3)
    assert tr["stop_used"] == pytest.approx(P(D1) - 2.0 - 0.25 * atr) == pytest.approx(log["stop"])
    assert tr["take_profit"] == pytest.approx(tr["entry_price"] + 2 * (tr["entry_price"] - tr["stop_used"]))
    assert tr["risk_usdt"] == pytest.approx(100.0) and log["fee_share"] < 0.2
    assert tr["exit_type"] == "take-profit" and tr["exit_ms"] == D1 + 8 * Q and "Autorun: hammer" in tr["notes"]

    # the thin zone: confirmed, the fees would eat more than 20 % of the risk
    fee_row = rows[2]
    assert fee_row["pattern"] == "hammer" and fee_row["fee_share"] >= 0.2 and fee_row["trade_id"] is None
    assert fee_row["est_fees"] == pytest.approx(fee_row["fee_share"] * 100.0)
    assert rows[5]["confirm_ms"] == D4 + 3 * Q                                 # the candle that closed below the zone
    assert status["trades_opened"] == 1 and status["trades_closed"] == 1 and status["outcomes"]["traded"] == 1

    # the report: funnel, trades and what-ifs, split by the daily trend
    rep = ar.report(sid)
    assert rep["signals"] == 6 and sum(rep["funnel"]["all"].values()) == 6
    assert sum(sum(rep["funnel"][k].values()) for k in ("above", "below", "unknown")) == 6
    assert rep["trades"]["all"]["count"] == 1 and rep["trades"]["all"]["total_r"] == pytest.approx(tr["r_multiple"])
    rows = ar.signals(sid)
    assert rows[0]["raw_result"] == "target" and rows[0]["raw_r"] == pytest.approx(2.0)   # entering at the signal close
    assert rows[0]["raw_entry"] == pytest.approx(P(D1))
    assert rows[0]["conf_result"] == "target" and rows[0]["conf_net_r"] == pytest.approx(tr["r_multiple"], abs=0.01)
    assert rows[2]["conf_result"] == "stop" and rows[2]["conf_exit_ms"] == D4 + 3 * Q   # the D4 break takes its stop
    assert rows[2]["conf_net_r"] < -1.0                                        # a loss of 1 R plus the fees
    assert rows[3]["conf_result"] is None and rows[3]["raw_result"] in ("open", "stop", "target")
    assert rep["what_if_confirmed"]["all"]["count"] == 2 and rep["what_if_confirmed"]["all"]["wins"] == 1
    csv = ar.signals_csv(sid)
    assert csv.splitlines()[0].startswith("seq,") and "fees_too_high" in csv and len(csv.splitlines()) == 7

    day.reset(sid)                                           # a reset clears the run and its log
    with pytest.raises(Conflict):
        ar.status(sid)
    with day.store.tx() as conn:
        assert day.store.list_signals(conn, sid) == []


def test_engulfing_confirms(env):
    day, ar, sid, fake = env
    fake[:] = [sig(D3, P(D3) + 0.5, P(D3) - 2.0)]
    ar.start(sid, {"hammer": False, "pictures": False})
    run_all(day, ar, sid)
    row = ar.signals(sid)[0]
    # the fake's wave turns up again 8 candles after 06:00: a green candle covering the red one before
    assert row["outcome"] == "traded" and row["pattern"] == "engulfing" and row["confirm_ms"] == D3 + 8 * Q


def test_trade_limit_and_entry_end(env):
    day, ar, sid, fake = env
    fake[:] = [sig(D1, P(D1) + 0.5, P(D1) - 2.0), sig(D3 + 3 * H, P(D3) + 0.5, P(D3) - 2.0)]
    ar.start(sid, {"engulfing": False, "pictures": False, "max_trades": 1})
    st = run_all(day, ar, sid)[-1]
    assert st["autorun"]["done"] == "1 trades taken" and len(ar.signals(sid)) == 1

    day.reset(sid)
    ar.start(sid, {"engulfing": False, "pictures": False, "entry_until": "2018-05-12"})   # D1 = 05-11 only
    st = run_all(day, ar, sid)[-1]
    assert st["autorun"]["done"] == "entry period over (2018-05-12)" and len(ar.signals(sid)) == 1


def test_open_trade_at_the_end_of_the_data(env):
    day, ar, sid, fake = env
    fake[:] = [sig(D4 + 2 * H, P(D4) + 0.5, P(D4) - 2.0)]      # after the D4 break; an engulfing at +8, the target is never reached
    ar.start(sid, {"hammer": False, "pictures": False})
    st = run_all(day, ar, sid)[-1]
    assert st["autorun"]["done"] == "end of the data, a trade is still open"
    assert ar.report(sid)["open_trades"] == 1


def test_signal_zone_comes_back_after_midnight(env):
    day, ar, sid, fake = env
    late = START + DAY + 22 * H                                  # 22:00: the window runs past 00:00
    fake[:] = [sig(late, P(late) + 0.5, P(late) - 2.0)]
    ar.start(sid, {"hammer": False, "pictures": False})
    run_all(day, ar, sid)
    row = ar.signals(sid)[0]
    assert row["outcome"] == "traded" and row["confirm_ms"] == late + 8 * Q   # the next day
    tr = day.trades(sid)["trades"][0]
    assert tr["zone_upper"] == pytest.approx(P(late) + 0.5)


def test_pictures_stop_the_run_for_the_frontend(env):
    day, ar, sid, fake = env
    fake[:] = [sig(D1, P(D1) + 0.5, P(D1) - 2.0), sig(D3, P(D3) + 0.5, P(D3) - 2.0)]
    ar.start(sid, {"engulfing": False})
    st = ar.advance(sid)
    assert st["autorun"]["need"] == "day_picture" and st["cursor_ms"] == START   # the jump leaves day 0: its picture first
    calls = run_all(day, ar, sid, pictures=True)
    assert any(e["kind"] == "picture" for c in calls for e in c["events"])
    assert any(e["kind"] == "autorun_buy" and e["n"] == 1 for c in calls for e in c["events"])
    tr = day.trades(sid)["trades"][0]
    assert tr["has_image"] is True                                 # taken at 24:00 of the exit day
    days = {d["date"]: d for d in day.days(sid)["days"]}
    assert all(d["has_image"] for date, d in days.items() if d["left_how"])   # every day left has its picture
    assert calls[-1]["autorun"]["phase"] == "done"


# ------------------------------------------------------------------ API
def test_autorun_api(tmp_path: Path, monkeypatch):
    client = TestClient(main.app)
    monkeypatch.setattr(main.store, "client", __import__("tests.fakes", fromlist=["FineFakeClient"]).FineFakeClient(NOW))
    from datetime import datetime, timezone
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()  # noqa: E731
    r = client.post("/api/day/sessions", json={"symbol": "TRXUSDT", "start": iso(START), "end": iso(END), "name": "CTest__01"})
    sid = r.json()["id"]
    try:
        base = f"/api/day/sessions/{sid}/autorun"
        assert client.get(base).status_code == 409                  # no run yet
        assert client.post(base, json={"rules": {"nope": 1}}).status_code == 422
        monkeypatch.setattr(main.day, "_strategy_signals", lambda s, js, p: [])
        assert client.post(base, json={"rules": {"pictures": False}}).json()["phase"] == "idle"
        st = client.post(f"{base}/advance", json={}).json()
        assert st["autorun"]["phase"] == "done" and st["autorun"]["need"] is None
        assert client.get(f"{base}/report").json()["signals"] == 0
        assert client.get(f"{base}/signals").json() == []
        assert client.get(f"{base}/signals.csv").text.startswith("seq")
    finally:
        client.delete(f"/api/sessions/{sid}")
