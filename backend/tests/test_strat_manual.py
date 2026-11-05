"""Manual mode: the strategy proposes the trades, the trader takes or skips them (strat_run.py).

The same search as Automatic finds the entry points, so the first test plays a Manual session that
takes everything next to an Automatic one. Then the proposal itself, Skip, the trader's own tools
during a trade, the strategy's exit signal as a notice, what a Manual session refuses, its ends,
and the HTTP routes."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.replay.day_engine import DayEngine
from app.replay.engine import Conflict, Invalid
from app.replay.strat import ENDED_BY_TIMEOUT, ENDED_BY_TRADE_LIMIT, ENDED_BY_USER, StrategySessions
from app.replay.strat_run import StrategyRun
from app.strategies.plugins._rline_retest import RLineRetest
from tests.fakes import DAY, H, Q, FineFakeClient
from tests.test_autorun import D1, D3, OVERRIDES, P, sig, signals
from tests.test_day import END, NOW, PNG, START, make
from tests.test_strat_run import CONFIRMED, SIGNAL, SIGNAL_COLS, TRADE_COLS, extra_strategies, run_all, same  # noqa: F401
from tests.test_strat_run import setup as automatic


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "replay.db"


def setup(db: Path, monkeypatch, fake: list[dict] | None, overrides: dict | None = None, strategy: str = CONFIRMED,
          max_trades: int = 100, hours: float = 0.0, pictures: bool = False) -> tuple[DayEngine, StrategySessions, StrategyRun, str]:
    """A prepared Manual session whose RLine_Retest plug-in finds the scripted signals `fake`."""
    day = make(db, OVERRIDES if overrides is None else overrides)
    strat = StrategySessions(day)
    if fake is not None:
        monkeypatch.setattr(RLineRetest, "find_signals", lambda self, candles: fake)
    sid = strat.create("BTCUSDT", START, END, run_mode="manual", strategy=strategy, max_trades=max_trades,
                       time_limit_hours=hours, pictures=pictures)["id"]
    day.prepare(sid)
    return day, strat, StrategyRun(strat), sid


def hold(day: DayEngine, sid: str) -> dict:
    """The trader steps candle by candle until the open trade has closed."""
    for _ in range(2000):
        st = day.step(sid, 1)
        if st["day"]["trade"] is None:
            return st
    raise AssertionError("the trade never closed")


def play(day: DayEngine, runner: StrategyRun, sid: str, answer=lambda proposal: 7, pictures: bool = False) -> list[dict]:
    """Stand in for the trader and the browser until the run is done: every proposal is answered
    (`answer` gives the confidence of a take, or None for a skip), a taken trade is stepped to its
    exit, the trade pictures asked for are taken."""
    calls = []
    for _ in range(2000):
        st = runner.advance(sid)
        calls.append(st)
        if st["run"]["need"]:
            assert pictures, "a picture was asked for with screenshots off"
            day.attach_image(sid, st["day"]["picture_due"]["id"], PNG)
        elif st["run"]["phase"] == "done":
            return calls
        elif st["run"]["proposal"]:
            confidence = answer(st["run"]["proposal"])
            if confidence is None:
                runner.skip(sid)
            else:
                runner.take(sid, confidence)
                hold(day, sid)
    raise AssertionError("the run did not finish")


def to_proposal(runner: StrategyRun, sid: str) -> dict:
    for _ in range(200):
        st = runner.advance(sid)
        if st["run"]["proposal"]:
            return st
        assert st["run"]["phase"] != "done", "the run finished without a proposal"
    raise AssertionError("no proposal came")


# ------------------------------------------------ the same entry points
def test_taking_every_proposal_trades_like_automatic(db: Path, tmp_path: Path, monkeypatch):
    a_day, _, a_runner, a_sid = automatic(tmp_path / "auto.db", monkeypatch, signals())
    run_all(a_day, a_runner, a_sid)
    day, strat, runner, sid = setup(db, monkeypatch, signals())
    calls = play(day, runner, sid)

    rows, a_rows = runner.signals(sid), a_runner.signals(a_sid)
    assert [r["outcome"] for r in rows] == [r["outcome"] for r in a_rows] and "fees_too_high" in [r["outcome"] for r in rows]
    for r, a in zip(rows, a_rows):
        same(r, a, SIGNAL_COLS)                           # the filters apply in both modes
    trades, a_trades = day.trades(sid)["trades"], a_day.trades(a_sid)["trades"]
    assert len(trades) == len(a_trades) >= 2
    for t, a in zip(trades, a_trades):
        same(t, a, TRADE_COLS)
        assert (t["run_mode"], t["confidence"]) == ("manual", 7) and a["run_mode"] == "automatic"
    proposals = [e["proposal"] for c in calls for e in c["events"] if e["kind"] == "proposal"]
    assert [p["cursor_ms"] for p in proposals] == [t["entry_ms"] for t in trades]   # one proposal per entry point
    b = calls[-1]["day"]["session"]
    assert b["end_flag"] == ENDED_BY_TIMEOUT and b["run"]["done"] and b["run"]["proposal"] is None


# ---------------------------------------------------------- the proposal
def test_the_run_stops_with_a_proposal_and_take_buys_it_as_proposed(db: Path, monkeypatch):
    fake = [sig(D1, P(D1) + 0.5, P(D1) - 2.0), sig(D3 + 3 * H, P(D3) + 0.5, P(D3) - 2.0)]
    day, strat, runner, sid = setup(db, monkeypatch, fake)
    before = day.state(sid)["day"]["session"]["run"]
    assert (before["started"], before["phase"], before["can_run"], before["proposal"], before["exit_notice"]) == (False, "new", True, None, None)

    st = to_proposal(runner, sid)
    p, run = st["run"]["proposal"], st["run"]
    assert st["cursor_ms"] == D1 + 2 * Q == p["cursor_ms"]            # the hammer two candles after the signal
    assert (run["phase"], run["phase_label"], run["can_run"]) == ("proposal", "Proposal: take or skip", False)
    assert p["entry"] == st["price"] and p["stop"] < p["entry"] < p["take_profit"] and p["rr"] == pytest.approx(2.0)
    assert (p["code"], p["text"], p["levels_kind"]) == ("hammer", "A hammer at the zone.", "atr") and "Target: 2 R." in p["levels_text"]
    assert p["risk_usdt"] == pytest.approx(100.0, rel=0.01) and p["coins"] * p["entry"] == pytest.approx(p["position_value"])
    assert p["stop_pct"] < 0 < p["tp_pct"] and (p["zone_top"], p["zone_bottom"]) == (pytest.approx(P(D1) + 0.5), pytest.approx(P(D1) - 2.0))
    assert [e["kind"] for e in st["events"]][-1] == "proposal" and st["day"]["trade"] is None
    (row,) = runner.signals(sid)                                       # the log row waits for the answer, the plan is on it
    assert row["outcome"] is None and (row["entry"], row["stop"], row["take_profit"]) == (p["entry"], p["stop"], p["take_profit"])
    assert len(day.zones(sid)) == 1

    again = runner.advance(sid)                                        # nothing moves until the answer
    assert again["cursor_ms"] == st["cursor_ms"] and again["run"]["proposal"] == p and again["events"] == []
    for bad in (None, 11, -1, 5.0, True, "7"):
        with pytest.raises(Invalid, match="confidence from 0 to 10"):
            runner.take(sid, bad)

    res = runner.take(sid, 8, notes="  Looks clean.  ")
    tr = res["trade"]
    assert (tr["entry_ms"], tr["entry_price"], tr["stop_used"], tr["take_profit"]) == (p["cursor_ms"], p["entry"], p["stop"], p["take_profit"])
    assert (tr["run_mode"], tr["strategy_key"], tr["confidence"], tr["coins"]) == ("manual", CONFIRMED, 8, pytest.approx(p["coins"]))
    assert tr["notes"].startswith("Looks clean. | Manual (RLine_Retest confirmed v1): A hammer at the zone.")
    run = res["state"]["day"]["session"]["run"]
    assert (run["phase"], run["can_run"], run["proposal"]) == ("trade", False, None)
    (row,) = runner.signals(sid)
    assert (row["outcome"], row["trade_id"]) == ("traded", tr["id"])
    for call in (lambda: runner.take(sid, 8), lambda: runner.skip(sid)):
        with pytest.raises(Conflict, match="No proposal is waiting"):
            call()


def test_the_trader_holds_the_trade_and_the_next_entry_point_comes_by_itself(db: Path, monkeypatch):
    fake = [sig(D1, P(D1) + 0.5, P(D1) - 2.0), sig(D3 + 3 * H, P(D3) + 0.5, P(D3) - 2.0)]
    day, strat, runner, sid = setup(db, monkeypatch, fake)
    p = to_proposal(runner, sid)["run"]["proposal"]
    tr = runner.take(sid, 5)["trade"]
    held = runner.advance(sid)                                         # the run does not step a Manual trade
    assert held["cursor_ms"] == p["cursor_ms"] and held["run"]["phase"] == "trade" and held["day"]["trade"]["id"] == tr["id"]

    # the trader's own tools: a moved stop is logged with the old and the new value, a quarter is sold
    day.step(sid, 1)
    moved = day.move_trade(sid, stop=p["stop"] + 0.25)["trade"]
    (change,) = moved["changes"]
    assert (change["field"], change["from"], change["to"]) == ("stop", pytest.approx(p["stop"]), pytest.approx(p["stop"] + 0.25))
    assert change["at_ms"] == p["cursor_ms"] + Q and change["at_local"] and change["price"] > 0
    day.scale_trade(sid, "sell", "stalls")
    st = hold(day, sid)
    assert st["cursor_ms"] == D1 + 8 * Q and st["day"]["session"]["run"]["can_run"] is True   # closed: the run may go on

    nxt = to_proposal(runner, sid)                                     # ... to the next entry point, without a question
    assert nxt["cursor_ms"] > D3 + 3 * H - Q and nxt["day"]["exit_choice"] is False and nxt["day"]["phase"] == "trading"
    assert [r["outcome"] for r in runner.signals(sid)] == ["traded", None]
    (first,) = day.trades(sid)["trades"]
    assert first["status"] == "closed" and first["exit_type"] == "take-profit" and len(day.zones(sid)) == 1


def test_a_skip_is_logged_and_does_not_count_toward_the_trade_limit(db: Path, monkeypatch):
    fake = [sig(D1, P(D1) + 0.5, P(D1) - 2.0), sig(D3 + 3 * H, P(D3) + 0.5, P(D3) - 2.0)]
    day, strat, runner, sid = setup(db, monkeypatch, fake, max_trades=1)
    first = to_proposal(runner, sid)
    st = runner.skip(sid)
    b = st["day"]["session"]
    assert (b["trades_taken"], b["ended"], b["run"]["phase"], b["run"]["can_run"]) == (0, False, "idle", True)
    (row,) = runner.signals(sid)
    assert (row["outcome"], row["outcome_label"], row["trade_id"]) == ("skipped", "Skipped by the trader", None)
    assert row["entry"] == first["run"]["proposal"]["entry"]           # what was passed on stays in the log

    calls = play(day, runner, sid)                                     # the second one is taken: that is trade 1 of 1
    b = calls[-1]["day"]["session"]
    assert (b["end_flag"], b["trades_taken"]) == (ENDED_BY_TRADE_LIMIT, 1)
    assert [r["outcome"] for r in runner.signals(sid)] == ["skipped", "traded"]
    assert {o["key"]: o["count"] for o in b["run"]["outcomes"]} == {"skipped": 1, "traded": 1}
    assert day.trades(sid)["trades"][0]["entry_ms"] > D3


def test_pictures_are_asked_for_in_manual_too(db: Path, monkeypatch):
    day, strat, runner, sid = setup(db, monkeypatch, [sig(D1, P(D1) + 0.5, P(D1) - 2.0)], max_trades=1, pictures=True)
    calls = play(day, runner, sid, pictures=True)
    (tr,) = day.trades(sid)["trades"]
    assert tr["has_image"] and any(c["run"]["need"] == "trade_picture" for c in calls)
    assert not any(d["has_image"] for d in day.days(sid)["days"])      # trade pictures only


# ------------------------------------------- the strategy's exit: a notice
def test_the_break_exit_is_only_a_notice_in_manual(db: Path, monkeypatch):
    brk = D3 + 5 * Q                     # a close below the D3 zone, with the low above the hard stop
    over = {brk: (P(D3 + 4 * Q), P(D3 + 4 * Q) + 0.1, P(D3) - 2.2, P(D3) - 2.1)}
    day, strat, runner, sid = setup(db, monkeypatch, [sig(D3, P(D3) + 0.5, P(D3) - 2.0)], overrides=over, strategy=SIGNAL)
    real = day._daily_ema
    monkeypatch.setattr(day, "_daily_ema", lambda s: {**real(s), "ema_above": 1})
    st = to_proposal(runner, sid)
    assert st["cursor_ms"] == D3 and st["run"]["proposal"]["code"] == "signal"   # this strategy enters at the signal candle
    tr = runner.take(sid, 6)["trade"]
    st = day.step(sid, 4)
    assert st["day"]["session"]["run"]["exit_notice"] is None
    st = day.step(sid, 1)                                              # Automatic sells here; Manual only says so
    note = st["day"]["session"]["run"]["exit_notice"]
    assert st["cursor_ms"] == brk and (note["code"], note["text"], note["cursor_ms"]) == ("setup_broken", "A candle closed below the zone.", brk)
    assert st["day"]["trade"]["id"] == tr["id"] and st["day"]["trade"]["status"] == "open"
    day.sell(sid, "setup_broken", "I agree with the strategy.")       # the trader's own close
    st = day.state(sid)
    assert st["day"]["session"]["run"]["exit_notice"] is None
    (done,) = day.trades(sid)["trades"]
    assert (done["exit_type"], done["exit_reason"], done["exit_ms"]) == ("early exit", "setup_broken", brk)


# ------------------------------------------------------------------ guards
def test_what_a_manual_session_takes_by_hand(db: Path, monkeypatch):
    a_day, a_strat, a_runner, a_sid = automatic(db.parent / "auto.db", monkeypatch, [])
    day, strat, runner, sid = setup(db, monkeypatch, [sig(D1, P(D1) + 0.5, P(D1) - 2.0)])
    plain = day.prepare(day.create_session("BTCUSDT", START, END)["id"])["id"]
    actions = ("buy", "step", "sell", "adjust", "leg", "jump", "back")
    for action in actions:
        with pytest.raises(Conflict, match="Automatic session"):
            a_strat.manual_guard(a_sid, action)
        strat.manual_guard(plain, action), strat.manual_guard("no-such-session", action)   # not strategy sessions

    refused = {"buy": "take or skip", "leg": "exits A and C are not used", "back": "no Step back",
               "jump": "moves to the next entry point by itself", "step": "step only while a trade is open"}
    for action, words in refused.items():
        with pytest.raises(Conflict, match=words):
            strat.manual_guard(sid, action)
    strat.manual_guard(sid, "sell"), strat.manual_guard(sid, "adjust")   # the engine itself says "no trade is open"
    to_proposal(runner, sid)
    with pytest.raises(Conflict, match="step only while a trade is open"):
        strat.manual_guard(sid, "step")                                # a proposal waits: answer it
    runner.take(sid, 3)
    strat.manual_guard(sid, "step")                                    # the trade is open: the trader moves the replay
    for action in ("buy", "leg", "jump", "back"):
        with pytest.raises(Conflict):
            strat.manual_guard(sid, action)
    with pytest.raises(Conflict, match="Only a Manual session"):
        a_runner.take(a_sid, 5)
    with pytest.raises(Conflict, match="not a strategy session"):
        runner.skip(plain)


# -------------------------------------------------------------- the ends
def test_the_time_limit_ends_a_session_with_an_unanswered_proposal(db: Path, monkeypatch):
    day, strat, runner, sid = setup(db, monkeypatch, [sig(D1, P(D1) + 0.5, P(D1) - 2.0)], hours=0.05)   # 3 minutes
    to_proposal(runner, sid)
    strat.add_clock(sid, 100), strat.add_clock(sid, 100)
    st = day.state(sid)
    run = st["day"]["session"]["run"]
    assert st["day"]["session"]["end_flag"] == ENDED_BY_TIMEOUT and run["can_run"] is True   # the run still has to close up
    with pytest.raises(Conflict, match="The session has ended"):
        runner.take(sid, 5)
    with pytest.raises(Conflict, match="The session has ended"):
        runner.skip(sid)
    st = runner.advance(sid)
    assert st["run"]["phase"] == "done" and st["run"]["proposal"] is None and day.trades(sid)["trades"] == []
    (row,) = runner.signals(sid)
    assert (row["outcome"], row["outcome_text"]) == ("session_ended", "The session ended before the proposal was answered.")


def test_end_session_closes_the_manual_trade_at_market(db: Path, monkeypatch):
    day, strat, runner, sid = setup(db, monkeypatch, [sig(D1, P(D1) + 0.5, P(D1) - 2.0)])
    to_proposal(runner, sid)
    runner.take(sid, 9)
    day.step(sid, 2)
    st = strat.stop(sid)
    assert st["day"]["session"]["end_flag"] == ENDED_BY_USER and st["day"]["session"]["run"]["can_run"] is True
    (tr,) = day.trades(sid)["trades"]
    assert (tr["exit_type"], tr["exit_ms"]) == ("session stopped", D1 + 4 * Q)
    assert runner.advance(sid)["run"]["phase"] == "done"
    assert day.state(sid)["day"]["session"]["run"]["can_run"] is False


def test_the_replays_end_closes_the_trade_the_trader_holds(db: Path, monkeypatch):
    from tests.test_autorun import D4
    day, strat, runner, sid = setup(db, monkeypatch, [sig(D4 + 2 * H, P(D4) + 0.5, P(D4) - 2.0)])   # the target is never reached
    calls = play(day, runner, sid)
    (tr,) = day.trades(sid)["trades"]
    st = calls[-1]
    assert tr["exit_type"] == "session timeout" and tr["exit_ms"] == st["data_end_ms"] == st["cursor_ms"]
    assert st["day"]["session"]["end_flag"] == ENDED_BY_TIMEOUT


def test_reset_clears_the_proposal(db: Path, monkeypatch):
    day, strat, runner, sid = setup(db, monkeypatch, [sig(D1, P(D1) + 0.5, P(D1) - 2.0)])
    first = to_proposal(runner, sid)["run"]["proposal"]
    st = day.reset(sid)
    run = st["day"]["session"]["run"]
    assert (run["started"], run["proposal"], run["can_run"]) == (False, None, True) and runner.signals(sid) == []
    with pytest.raises(Conflict, match="No proposal is waiting"):
        runner.take(sid, 5)
    again = to_proposal(runner, sid)["run"]["proposal"]               # the same entry point comes again
    assert {**again, "zone_id": None} == {**first, "zone_id": None}


# --------------------------------------------------------------------- API
SYMBOL = "DOGEUSDT"   # a coin no other API test module downloads


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


@pytest.fixture(scope="module")
def client() -> TestClient:
    main.store.client = FineFakeClient(NOW)   # no network during tests
    return TestClient(main.app)


def test_api_take_skip_and_what_manual_refuses(client: TestClient, extra_strategies):  # noqa: F811
    """The test strategy buys every 06:00 close: stop 1 % below, target 2 % above."""
    body = {"symbol": SYMBOL, "start": iso(START), "end": iso(END), "run_mode": "manual", "strategy": "test_morning",
            "max_trades": 2, "pictures": False}
    sid = client.post("/api/strategy/sessions", json=body).json()["id"]
    try:
        day_api, strat_api = f"/api/day/sessions/{sid}", f"/api/strategy/sessions/{sid}"
        assert client.post(f"{strat_api}/take", json={"confidence": 5}).status_code == 409    # nothing proposed yet
        assert client.post(f"{strat_api}/skip").status_code == 409
        r = client.post(f"{day_api}/step", json={"n": 1})
        assert r.status_code == 409 and "step only while a trade is open" in r.json()["detail"]

        st = client.post(f"{strat_api}/run", json={}).json()
        p = st["run"]["proposal"]
        assert st["cursor_ms"] == START + 6 * H and p["entry"] == st["price"] and st["run"]["phase"] == "proposal"
        assert client.get(day_api).json()["day"]["session"]["run"]["proposal"] == p             # a reload shows it again
        assert client.post(f"{strat_api}/skip").json()["day"]["session"]["run"]["phase"] == "idle"
        st = client.post(f"{strat_api}/run", json={}).json()                                    # the next morning
        p = st["run"]["proposal"]
        assert st["cursor_ms"] == START + DAY + 6 * H

        r = client.post(f"{day_api}/buy", json={"zone_id": p["zone_id"], "take_profit": p["take_profit"], "confidence": 5})
        assert r.status_code == 409 and "take or skip" in r.json()["detail"]
        for bad in ({}, {"confidence": 11}, {"confidence": "5"}, {"confidence": 5, "notes": "x" * 201}):
            assert client.post(f"{strat_api}/take", json=bad).status_code == 422, bad
        r = client.post(f"{strat_api}/take", json={"confidence": 4, "notes": "Second morning."})
        tr = r.json()["trade"]
        assert r.status_code == 200 and (tr["entry_price"], tr["confidence"], tr["run_mode"]) == (p["entry"], 4, "manual")
        assert r.json()["state"]["day"]["session"]["trades_taken"] == 1

        for path, payload, words in (("step-back", {}, "no Step back"), ("skip", None, "next entry point by itself"),
                                     ("goto", {"date": "2018-05-14"}, "next entry point"), ("next-signal", {}, "next entry point"),
                                     ("end-day", None, "next entry point"), ("sell", {"reason": "news", "exits": "A"}, "exits A and C")):
            r = client.post(f"{day_api}/{path}", json=payload) if payload is not None else client.post(f"{day_api}/{path}")
            assert r.status_code == 409 and words in r.json()["detail"], path
        assert client.post(f"/api/sessions/{sid}/step", json={"interval": "15m", "n": 1}).status_code == 409
        assert client.post(f"{day_api}/step", json={"n": 1}).status_code == 200                 # the trader holds the trade
        assert client.patch(f"{day_api}/trade", json={"stop": tr["stop_used"] * 0.999}).status_code == 200
        assert client.post(f"{day_api}/scale", json={"side": "sell", "reason": "stalls"}).status_code == 200
        held = client.post(f"{strat_api}/run", json={}).json()
        assert held["run"]["phase"] == "trade" and held["cursor_ms"] == st["cursor_ms"] + Q    # the run leaves it alone
        assert client.post(f"{day_api}/sell", json={"reason": "news"}).status_code == 200       # a close by hand
        rows = client.get(f"{strat_api}/signals").json()
        assert [r["outcome"] for r in rows[:2]] == ["skipped", "traded"]
        st = client.post(f"{strat_api}/run", json={}).json()                                    # on to the next entry point
        assert st["run"]["phase"] == "proposal" and st["cursor_ms"] == START + 2 * DAY + 6 * H
    finally:
        client.delete(f"/api/sessions/{sid}")
