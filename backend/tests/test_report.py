"""The Report tab (report.py): the numbers, the modes of old and new sessions, the filters, the CSV.

One database holds the four kinds of sessions the report knows: an Automatic strategy session, a
Manual one, an old day session with an autorun (a CTest__NN run) and an old day session traded by
hand. The Automatic session and the old autorun trade the same scripted signals with the same rules,
so their numbers must be the same."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.replay.autorun import Autorun
from app.replay.engine import Invalid
from app.replay.report import CSV_COLUMNS, Report, stats
from app.replay.strat import ENDED_BY_TIMEOUT, ENDED_BY_TRADE_LIMIT, StrategySessions
from app.replay.strat_run import StrategyRun
from app.strategies.plugins._rline_retest import RLineRetest
from tests.fakes import DAY, Q, FineFakeClient
from tests.test_autorun import D1, OVERRIDES, P, signals
from tests.test_autorun import run_all as autorun_all
from tests.test_day import END, NOW, START, make
from tests.test_strat_manual import hold, to_proposal
from tests.test_strat_run import CONFIRMED, run_all

NUMBERS = ("trades", "wins", "losses", "breakevens", "win_rate", "avg_win_r", "avg_loss_r", "avg_win_usdt", "avg_loss_usdt",
           "expectancy_r", "total_usdt", "total_pct", "total_r", "avg_hours", "median_hours", "max_drawdown_r",
           "win_streak", "loss_streak")


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> dict:
    """The four sessions, each run to its end (the hand-traded one has one closed trade). Built once
    for the module: the tests only read them."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield build(tmp_path_factory.mktemp("report") / "replay.db", monkeypatch)


def build(db: Path, monkeypatch) -> dict:
    day = make(db, OVERRIDES)
    strat = StrategySessions(day)
    runner = StrategyRun(strat)
    monkeypatch.setattr(RLineRetest, "find_signals", lambda self, candles: signals())
    monkeypatch.setattr(day, "_strategy_signals", lambda s, js, p: signals())

    auto = strat.create("BTCUSDT", START, END, run_mode="automatic", strategy=CONFIRMED, max_trades=100, pictures=False)["id"]
    day.prepare(auto)
    run_all(day, runner, auto)

    manual = strat.create("BTCUSDT", START, END, run_mode="manual", strategy=CONFIRMED, max_trades=1, pictures=False)["id"]
    day.prepare(manual)
    p = to_proposal(runner, manual)["run"]["proposal"]
    runner.take(manual, 8)
    day.step(manual, 1)
    day.move_trade(manual, stop=p["stop"] + 0.25)   # a stop move, logged with the trade
    hold(day, manual)
    for _ in range(50):
        if runner.advance(manual)["run"]["phase"] == "done":
            break

    old = day.prepare(day.create_session("BTCUSDT", START, END, start_balance=100_000, name="CTest__01")["id"])["id"]
    ar = Autorun(day)
    ar.start(old, {"pictures": False})   # CTest__01's rules: the defaults
    autorun_all(day, ar, old)

    hand = day.prepare(day.create_session("BTCUSDT", START, END, start_balance=100_000, name="October")["id"])["id"]
    day.jump_to(hand, D1 + Q, "To the morning.")
    z = day.add_zone(hand, "1h", P(D1) + 0.5, P(D1) - 0.5)
    day.buy(hand, z["id"], take_profit=P(D1 + Q) + 50.0, stop=P(D1 + Q) - 50.0, confidence=4)
    day.step(hand, 2)
    day.sell(hand, "news")
    return {"day": day, "report": Report(day), "auto": auto, "manual": manual, "old": old, "hand": hand}


# ------------------------------------------------------------------ the numbers
def row(outcome: str | None, r: float | None, hours: float = 1.0, usdt: float | None = None) -> dict:
    return {"status": "open" if outcome is None else "closed", "outcome": outcome, "result_r": r,
            "pnl_usdt": None if r is None else (usdt if usdt is not None else r * 100.0), "duration_h": hours,
            "entry_ms": 0, "exit_ms": 0}


def test_stats_follow_the_definitions():
    rows = [row("WIN", 2.0, 1), row("WIN", 1.5, 3), row("LOSS", -1.0, 2), row("BREAKEVEN", 0.02, 10),
            row("LOSS", -1.0, 4), row("LOSS", -1.1, 5), row("WIN", 2.0, 6), row(None, None)]
    s = stats(rows, 100_000.0)
    assert (s["trades"], s["open"], s["wins"], s["losses"], s["breakevens"]) == (7, 1, 3, 3, 1)
    assert s["win_rate"] == pytest.approx(3 / 7 * 100)                      # breakevens count in the total
    assert s["avg_win_r"] == pytest.approx(5.5 / 3) and s["avg_loss_r"] == pytest.approx(-3.1 / 3)
    assert s["avg_win_usdt"] == pytest.approx(550 / 3) and s["avg_loss_usdt"] == pytest.approx(-310 / 3)
    assert s["expectancy_r"] == pytest.approx(2.42 / 7) and s["total_r"] == pytest.approx(2.42)
    assert s["total_usdt"] == pytest.approx(242.0) and s["total_pct"] == pytest.approx(0.242)
    assert s["avg_hours"] == pytest.approx(31 / 7) and s["median_hours"] == 4
    # running R: 2, 3.5, 2.5, 2.52, 1.52, 0.42, 2.42 -> the deepest fall from 3.5 is to 0.42
    assert s["max_drawdown_r"] == pytest.approx(3.08)
    assert (s["win_streak"], s["loss_streak"]) == (2, 2)                     # the breakeven ends the loss run
    empty = stats([row(None, None)])
    assert empty["trades"] == 0 and empty["win_rate"] is None and empty["total_usdt"] is None and empty["max_drawdown_r"] is None
    assert stats([row("LOSS", -1.0)], None)["total_pct"] is None            # no common start balance: no percent
    assert stats([row("LOSS", -1.0), row("LOSS", -0.5)])["max_drawdown_r"] == pytest.approx(1.5)   # the start (0) is the first top


# ------------------------------------------------------------- the four modes
def test_every_kind_of_day_session_has_its_mode(world):
    rep = world["report"].report()
    by_id = {s["id"]: s for s in rep["sessions"]}
    a, m, o, h = (by_id[world[k]] for k in ("auto", "manual", "old", "hand"))
    assert (a["mode"], a["strategy"], a["strategy_label"], a["end_flag"]) == ("automatic", CONFIRMED, "RLine_Retest confirmed v1", ENDED_BY_TIMEOUT)
    assert (m["mode"], m["mode_label"], m["end_flag"], m["end_label"]) == ("manual", "Manual", ENDED_BY_TRADE_LIMIT, "Trade limit reached")
    assert (o["mode"], o["strategy"], o["strategy_label"], o["end_flag"]) == ("automatic", "autorun:confirm", "Autorun (entry at the confirmation, 2 R)", None)
    assert (h["mode"], h["mode_label"], h["strategy"], h["end_label"]) == ("hand", "By hand", None, None)
    opts = rep["options"]
    assert [x["key"] for x in opts["modes"]] == ["automatic", "manual", "hand"] and opts["symbols"] == ["BTCUSDT"]
    assert {x["key"] for x in opts["strategies"]} == {CONFIRMED, "autorun:confirm"}
    assert rep["summary"]["trades"] == sum(s["stats"]["trades"] for s in rep["sessions"])


def test_the_automatic_session_and_the_old_autorun_give_the_same_numbers(world):
    rep = world["report"]
    a, o = rep.report(sessions=[world["auto"]]), rep.report(sessions=[world["old"]])
    assert a["summary"]["trades"] >= 2
    for k in NUMBERS:
        assert a["summary"][k] == pytest.approx(o["summary"][k]), k
    for x, y in zip(a["trades"], o["trades"]):
        assert (x["entry_time"], x["exit_time"], x["exit_kind"], x["outcome"]) == (y["entry_time"], y["exit_time"], y["exit_kind"], y["outcome"])
        assert y["strategy"] == "Autorun (entry at the confirmation, 2 R)" and x["strategy"] == "RLine_Retest confirmed" and x["strategy_version"] == "1"


def test_trade_rows_hold_the_spec_fields(world):
    rep = world["report"].report()
    m = next(t for t in rep["trades"] if t["session_id"] == world["manual"])
    assert (m["mode"], m["status"], m["confidence"], m["sl_tp_changes"]) == ("manual", "closed", 8, 1)
    (c,) = m["change_list"]
    assert c["field"] == "stop" and m["changes"].startswith(f"{c['at_local']} SL ") and m["final_sl"] == pytest.approx(c["to"])
    assert m["initial_sl"] == pytest.approx(c["from"]) and m["initial_sl"] < m["entry_price"] < m["initial_tp"]
    assert m["outcome"] in ("WIN", "LOSS", "BREAKEVEN") and m["result_r"] is not None and m["result_price"] == pytest.approx(m["exit_price"] - m["entry_price"])
    assert m["duration_h"] == pytest.approx((m["exit_ms"] - m["entry_ms"]) / 3_600_000)
    assert m["entry_time"][-2:] in ("00", "15", "30", "45")   # the close of a 15m candle
    assert (m["fee_rate"], m["slippage"], m["fill_model"]) == (pytest.approx(0.001), 0.0, "exact level")
    assert m["high_price"] >= m["entry_price"] >= m["low_price"] and m["high_time"] and m["mfe_r"] is not None
    h = next(t for t in rep["trades"] if t["session_id"] == world["hand"])
    assert (h["mode"], h["exit_kind"], h["exit_reason"], h["strategy"], h["confidence"]) == ("hand", "manual close", "News or an event", None, 4)
    kinds = {t["exit_kind"] for t in rep["trades"] if t["session_id"] == world["auto"]}
    assert kinds <= {"SL", "TP", "session timeout"} and "TP" in kinds


# ------------------------------------------------------------------- filters
def test_filters(world):
    rep = world["report"]
    ids = lambda r: {s["id"] for s in r["sessions"]}  # noqa: E731
    assert ids(rep.report(end="timeout")) == {world["auto"]}
    assert ids(rep.report(end="non_timeout")) == {world["manual"], world["old"], world["hand"]}   # old sessions have no flag
    assert ids(rep.report(mode="automatic")) == {world["auto"], world["old"]}
    assert ids(rep.report(mode="hand")) == {world["hand"]}
    assert ids(rep.report(strategy="autorun:confirm")) == {world["old"]}
    assert ids(rep.report(symbol="ETHUSDT")) == set() and rep.report(symbol="ETHUSDT")["summary"]["trades"] == 0
    assert ids(rep.report(sessions=[world["manual"], world["hand"]])) == {world["manual"], world["hand"]}

    every = rep.report(sessions=[world["auto"]])["trades"]
    first_day = every[0]["entry_date"]
    later = rep.report(sessions=[world["auto"]], date_from=(datetime.fromisoformat(first_day) + timedelta(days=1)).date().isoformat())
    assert all(t["entry_date"] > first_day for t in later["trades"]) and len(later["trades"]) < len(every)
    assert all(t["entry_date"] <= first_day for t in rep.report(date_to=first_day)["trades"])

    wins = rep.report(outcome="WIN")
    assert wins["trades"] and all(t["outcome"] == "WIN" for t in wins["trades"]) and wins["summary"]["losses"] == 0
    assert all(t["exit_kind"] == "TP" for t in rep.report(exit_kind="TP")["trades"])
    for bad in ({"end": "x"}, {"mode": "swing"}, {"outcome": "won"}, {"exit_kind": "x"}, {"date_from": "5.1.2024"},
                {"date_from": "2018-05-12", "date_to": "2018-05-11"}):
        with pytest.raises(Invalid):
            rep.report(**bad)


def test_csv_has_every_column_and_the_filtered_trades(world):
    rep = world["report"]
    body = rep.csv(mode="automatic")
    rows = list(csv.DictReader(io.StringIO(body)))
    assert tuple(rows[0].keys()) == CSV_COLUMNS
    assert len(rows) == len(rep.report(mode="automatic")["trades"])
    assert {r["mode"] for r in rows} == {"automatic"} and all(r["confidence"] == "" for r in rows)   # empty, not "None"
    assert {r["session_end"] for r in rows} == {ENDED_BY_TIMEOUT, ""}


# ----------------------------------------------------------------------- API
def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def test_api_report_and_csv(monkeypatch):
    main.store.client = FineFakeClient(NOW)
    client = TestClient(main.app)
    monkeypatch.setattr(RLineRetest, "find_signals", lambda self, candles: [])
    body = {"symbol": "BTCUSDT", "start": iso(START), "end": iso(START + 2 * DAY), "run_mode": "automatic", "strategy": CONFIRMED,
            "max_trades": 3, "pictures": False}
    sid = client.post("/api/strategy/sessions", json=body).json()["id"]
    try:
        for _ in range(100):
            if client.get(f"/api/day/sessions/{sid}").json()["status"] == "ready":
                break
        client.post(f"/api/strategy/sessions/{sid}/run", json={})        # no signal: straight to the end, ENDED_BY_TIMEOUT
        r = client.get("/api/report", params={"sessions": sid, "end": "timeout"})
        assert r.status_code == 200
        data = r.json()
        assert [s["id"] for s in data["sessions"]] == [sid] and data["summary"]["trades"] == 0 and data["trades"] == []
        assert data["filters"] == {"sessions": [sid], "end": "timeout"}
        assert client.get("/api/report", params={"end": "later"}).status_code == 422
        c = client.get("/api/report.csv", params={"sessions": sid})
        assert c.status_code == 200 and c.headers["content-type"].startswith("text/csv")
        assert c.text.splitlines() == [",".join(CSV_COLUMNS)] and "report_trades.csv" in c.headers["content-disposition"]
    finally:
        client.delete(f"/api/sessions/{sid}")
