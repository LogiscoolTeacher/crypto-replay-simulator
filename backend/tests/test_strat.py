"""Strategy sessions: the setup chosen before the start, the limits and the three end flags, and what
is stored with every trade (mode, strategy, the reproducibility fields, outcome, extremes, the 24
hours after the exit). Engine tests against the 15-minute fake exchange, then the HTTP routes."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.replay.day_engine import BREAKEVEN_R, FILL_MODEL, PRICE_SOURCE, DayEngine, outcome_of
from app.replay.engine import Conflict, Invalid, _iso
from app.replay.strat import (
    ENDED_BY_TIMEOUT,
    ENDED_BY_TRADE_LIMIT,
    ENDED_BY_USER,
    TIMEOUT_RULE,
    StrategySessions,
)
from app.strategies.plugin import get_trading_strategy
from tests.fakes import DAY, Q, FineFakeClient, quarter_candle
from tests.test_day import END, FIRST_CURSOR, NOW, START, make

PRICE = quarter_candle(FIRST_CURSOR)[3]   # the close of the first candle: every test buys there
C1, C2 = FIRST_CURSOR + Q, FIRST_CURSOR + 2 * Q
CONFIRMED = get_trading_strategy("rline_retest_confirmed")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "replay.db"


def setup(db: Path, overrides: dict | None = None, end: int = END, **kw) -> tuple[DayEngine, StrategySessions, str]:
    """A prepared strategy session (Automatic, the confirmed strategy, 3 trades unless told otherwise)."""
    day = make(db, overrides)
    strat = StrategySessions(day)
    args = {"run_mode": "automatic", "strategy": "rline_retest_confirmed", "max_trades": 3, **kw}
    sid = strat.create("BTCUSDT", START, end, **args)["id"]
    day.prepare(sid)
    return day, strat, sid


def buy(day: DayEngine, sid: str, stop: float = PRICE - 5.0, target: float = PRICE + 10.0) -> dict:
    z = day.add_zone(sid, "1h", PRICE + 0.5, PRICE - 0.5)
    return day.buy(sid, z["id"], take_profit=target, stop=stop)["trade"]


def run_to(day: DayEngine, sid: str, cursor_ms: int) -> dict:
    """Step until the cursor is at `cursor_ms` (a step also stops where a trade picture becomes due)."""
    st = day.state(sid)
    while st["cursor_ms"] < cursor_ms and not st["finished"]:
        st = day.step(sid, int((cursor_ms - st["cursor_ms"]) // Q))
    return st


# -------------------------------------------------------------------- setup
def test_create_stores_the_setup_and_reads_the_timeframe_from_the_strategy(db: Path):
    day, strat, sid = setup(db, run_mode="manual", max_trades=20, time_limit_hours=2.5, pictures=False)
    st = day.state(sid)
    assert st["mode"] == "day" and st["name"] == "Manual RLine_Retest confirmed BTCUSDT from 2018-05-10"
    b = st["day"]["session"]
    assert (b["run_mode"], b["run_mode_label"]) == ("manual", "Manual")
    assert b["strategy"] == {"key": "rline_retest_confirmed", "name": "RLine_Retest confirmed", "version": "1",
                             "label": "RLine_Retest confirmed v1", "timeframe": "1h",
                             "settings": CONFIRMED.settings(), "rules": CONFIRMED.rules_text()}
    assert (b["max_trades"], b["trades_taken"], b["trades_closed"], b["trades_left"]) == (20, 0, 0, 20)
    assert (b["time_limit_hours"], b["clock_seconds"], b["clock_left_seconds"]) == (2.5, 0.0, 9000.0)
    assert b["pictures"] is False and b["timeout_rule"] == TIMEOUT_RULE and b["timeout_rule_label"]
    assert (b["ended"], b["end_flag"], b["end_detail"], b["ended_at"], b["ended_local"], b["can_stop"]) == (False, None, None, None, None, True)
    assert st["day"]["entries_allowed"] is True
    auto = strat.create("BTCUSDT", START, END, run_mode="automatic", strategy="rline_retest_signal", max_trades=5, name="mine")
    assert auto["name"] == "mine" and auto["day"]["session"]["strategy"]["label"] == "RLine_Retest signal candle v1"
    assert auto["day"]["session"]["clock_left_seconds"] is None and auto["day"]["session"]["pictures"] is True


@pytest.mark.parametrize("kw, message", [
    ({"run_mode": "both"}, "run_mode must be one of"),
    ({"strategy": "nope"}, "unknown strategy 'nope'"),
    ({"max_trades": 0}, "maximum number of trades"),
    ({"max_trades": True}, "maximum number of trades"),
    ({"max_trades": 2.0}, "maximum number of trades"),
    ({"run_mode": "manual", "time_limit_hours": -1}, "time limit must be"),
    ({"run_mode": "manual", "time_limit_hours": float("nan")}, "time limit must be"),
    ({"run_mode": "automatic", "time_limit_hours": 2}, "ends at its end date"),
    ({"pictures": 1}, "pictures must be true or false"),
])
def test_create_rejects_a_bad_setup(db: Path, kw: dict, message: str):
    strat = StrategySessions(make(db))
    args = {"run_mode": "automatic", "strategy": "rline_retest_confirmed", "max_trades": 3, **kw}
    with pytest.raises(Invalid, match=message):
        strat.create("BTCUSDT", START, END, **args)
    with strat.day.sessions.tx() as conn:   # nothing half-made is left behind
        assert strat.day.sessions.list_sessions(conn) == []


def test_setup_options_list_the_registered_strategies():
    o = StrategySessions.setup_options()
    assert [m["key"] for m in o["modes"]] == ["automatic", "manual"]
    assert [s["key"] for s in o["strategies"]] == ["rline_retest_confirmed", "rline_retest_signal"]
    assert [f["key"] for f in o["end_flags"]] == [ENDED_BY_TRADE_LIMIT, ENDED_BY_TIMEOUT, ENDED_BY_USER]
    assert o["defaults"] == {"run_mode": "automatic", "max_trades": 100, "time_limit_hours": 0.0, "pictures": True}


def test_a_plain_day_session_has_no_session_block(db: Path):
    day = make(db)
    strat = StrategySessions(day)
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    assert day.state(sid)["day"]["session"] is None
    t = buy(day, sid)
    # the reproducibility fields are stored with every day trade; mode and strategy only in a strategy session
    assert (t["fee_rate"], t["slippage"], t["fill_model"], t["price_source"]) == (0.001, 0.0, FILL_MODEL, PRICE_SOURCE)
    assert (t["run_mode"], t["strategy_key"], t["strategy_name"], t["strategy_version"], t["timeframe"], t["timeout_rule"]) == (None,) * 6
    with pytest.raises(Conflict, match="not a strategy session"):
        strat.stop(sid)
    with pytest.raises(Conflict, match="not a strategy session"):
        strat.add_clock(sid, 5)
    run_to(day, sid, day.state(sid)["data_end_ms"])            # the end of the replay ends nothing here
    st = day.state(sid)
    assert st["finished"] and st["day"]["trade"] is not None and st["day"]["session"] is None


# ------------------------------------------------------------------- trades
def test_a_trade_stores_mode_strategy_and_what_its_result_depends_on(db: Path):
    day, _, sid = setup(db, run_mode="manual")
    t = buy(day, sid)
    assert (t["run_mode"], t["strategy_key"], t["strategy_name"], t["strategy_version"], t["timeframe"]) == (
        "manual", "rline_retest_confirmed", "RLine_Retest confirmed", "1", "1h")
    assert (t["fee_rate"], t["slippage"], t["fill_model"], t["price_source"], t["timeout_rule"]) == (
        0.001, 0.0, "exact level", "Binance spot candles (15m)", "close_at_market")
    assert t["outcome"] is None and t["extremes"] is None and t["after"] is None
    b = day.state(sid)["day"]["session"]
    assert (b["trades_taken"], b["trades_closed"], b["trades_left"]) == (1, 0, 2)


def test_outcome_rule():
    assert BREAKEVEN_R == 0.05
    assert [outcome_of(r) for r in (2.0, 0.051, 0.05, 0.0, -0.05, -0.051, -1.0)] == [
        "WIN", "WIN", "BREAKEVEN", "BREAKEVEN", "BREAKEVEN", "LOSS", "LOSS"]
    assert outcome_of(None) is None


def test_a_win_stores_outcome_gross_result_and_the_extremes(db: Path):
    day, _, sid = setup(db, {C1: (PRICE, PRICE + 1.0, PRICE - 2.5, PRICE),       # the lowest low
                             C2: (PRICE, PRICE + 11.0, PRICE - 1.0, PRICE)})     # the high takes the target out
    t = buy(day, sid)                                                             # stop -5 (20 coins), target +10
    tr = day.step(sid, 5)["events"][0]["trade"]
    assert tr["exit_type"] == "take-profit" and tr["exit_ms"] == C2 and tr["outcome"] == "WIN"
    assert tr["result_price"] == pytest.approx(10.0)
    assert tr["gross_pnl"] == pytest.approx(200.0) and tr["gross_r"] == pytest.approx(2.0)
    assert tr["gross_pnl"] == pytest.approx(tr["pnl"] + tr["fees"]) and tr["r_multiple"] < 2.0
    assert tr["gross_pnl_pct"] == pytest.approx(200.0 / t["position_value"] * 100.0)
    assert (tr["high_price"], tr["high_ms"], tr["low_price"], tr["low_ms"]) == (PRICE + 11.0, C2, PRICE - 2.5, C1)
    assert tr["extremes"] == {"high": PRICE + 11.0, "high_iso": _iso(C2), "low": PRICE - 2.5, "low_iso": _iso(C1)}
    assert _iso(C2).startswith("2018-05-10T00:30")
    assert tr["excursion"]["mfe_r"] == pytest.approx(11.0 / 5.0) and tr["excursion"]["mae_r"] == pytest.approx(-2.5 / 5.0)


def test_an_exit_at_the_entry_price_is_breakeven_after_fees(db: Path):
    day, _, sid = setup(db, {C1: (PRICE, PRICE + 0.2, PRICE - 0.2, PRICE)})
    buy(day, sid)
    day.step(sid, 1)
    tr = day.sell(sid, "stalls")["events"][0]["trade"]
    assert tr["exit_price"] == PRICE and tr["result_price"] == 0.0 and tr["gross_pnl"] == pytest.approx(0.0)
    assert -BREAKEVEN_R < tr["r_multiple"] < 0 and tr["outcome"] == "BREAKEVEN"     # only the fees were lost


def test_a_loss_at_the_stop(db: Path):
    day, _, sid = setup(db, {C1: (PRICE, PRICE + 0.2, PRICE - 6.0, PRICE)})
    buy(day, sid)
    tr = day.step(sid, 1)["events"][0]["trade"]
    assert tr["exit_type"] == "stop" and tr["outcome"] == "LOSS" and tr["gross_r"] == pytest.approx(-1.0) and tr["r_multiple"] < -1.0


def test_the_24_hours_after_the_exit_are_filled_once_the_replay_passed_them(db: Path):
    last = C2 + 96 * Q                                                            # the last candle of the window
    day, _, sid = setup(db, {C2: (PRICE, PRICE + 11.0, PRICE - 1.0, PRICE),
                             C2 + 10 * Q: (PRICE, PRICE + 20.0, PRICE - 1.0, PRICE),       # the window's high
                             C2 + 50 * Q: (PRICE, PRICE + 1.0, PRICE - 9.0, PRICE),        # the window's low
                             last + Q: (PRICE, PRICE + 50.0, PRICE - 50.0, PRICE)})        # one candle too late
    buy(day, sid)
    tr = day.step(sid, 5)["events"][0]["trade"]
    assert tr["exit_ms"] == C2 and tr["after"] is None
    run_to(day, sid, last - Q)
    assert day.trades(sid)["trades"][0]["after"] is None                           # not yet: nothing of the future is shown
    run_to(day, sid, last)
    a = day.trades(sid)["trades"][0]["after"]
    assert a["hours"] == 24 and a["complete"] is True
    assert (a["high"], a["low"]) == (PRICE + 20.0, PRICE - 9.0)
    assert a["high_iso"].startswith("2018-05-10T03:00") and a["low_iso"].startswith("2018-05-10T13:00")
    exit_price = PRICE + 10.0
    assert a["high_pct"] == pytest.approx((PRICE + 20.0) / exit_price * 100 - 100) and a["low_pct"] == pytest.approx((PRICE - 9.0) / exit_price * 100 - 100)
    row = day.trades(sid)["trades"][0]
    assert row["after_until_ms"] == last and row["after_high_ms"] == C2 + 10 * Q and row["after_low_ms"] == C2 + 50 * Q


def test_trades_closed_before_the_new_fields_get_them_when_listed(db: Path):
    day, _, sid = setup(db, {C1: (PRICE, PRICE + 1.0, PRICE - 2.5, PRICE), C2: (PRICE, PRICE + 11.0, PRICE - 1.0, PRICE)})
    buy(day, sid)
    tr = day.step(sid, 5)["events"][0]["trade"]
    with day.store.tx() as conn:   # as an old trade looks: none of the new result fields
        day.store.update_trade(conn, tr["id"], outcome=None, result_price=None, gross_pnl=None, gross_pnl_pct=None, gross_r=None,
                               high_price=None, high_ms=None, low_price=None, low_ms=None)
    again = day.trades(sid)["trades"][0]
    for k in ("outcome", "result_price", "gross_pnl", "gross_pnl_pct", "gross_r", "high_price", "high_ms", "low_price", "low_ms"):
        assert again[k] == pytest.approx(tr[k]), k


# --------------------------------------------------------------- end flags
def test_trade_limit_ends_the_session_when_the_last_trade_has_closed(db: Path):
    day, _, sid = setup(db, {C1: (PRICE, PRICE + 0.2, PRICE - 6.0, PRICE)}, max_trades=1)
    buy(day, sid)
    b = day.state(sid)["day"]["session"]
    assert (b["trades_taken"], b["trades_left"], b["ended"]) == (1, 0, False)      # the last trade is still open
    st = day.step(sid, 1)
    assert st["events"][0]["exit_type"] == "stop"
    b = st["day"]["session"]
    assert (b["ended"], b["end_flag"], b["end_flag_label"], b["end_detail"]) == (True, ENDED_BY_TRADE_LIMIT, "Trade limit reached", "1 trade taken")
    assert b["ended_at"] and b["ended_local"] == "2018-05-10 00:30" and b["can_stop"] is False
    assert st["day"]["entries_allowed"] is False and st["day"]["block_reason"] == "The session has ended (1 trade taken)."
    assert st["day"]["phase"] == "trading" and st["day"]["exit_choice"] is False   # no "end the day or keep trading" any more
    with pytest.raises(Conflict, match="The session has ended"):
        buy(day, sid)


def test_the_session_goes_on_while_trades_are_left(db: Path):
    day, _, sid = setup(db, {C1: (PRICE, PRICE + 0.2, PRICE - 6.0, PRICE)}, max_trades=2)
    buy(day, sid)
    st = day.step(sid, 1)
    b = st["day"]["session"]
    assert (b["trades_taken"], b["trades_closed"], b["trades_left"], b["ended"]) == (1, 1, 1, False)
    assert st["day"]["entries_allowed"] is True


def test_the_end_of_the_replay_is_a_timeout_and_closes_an_open_trade_at_market(db: Path):
    day, _, sid = setup(db, end=START + DAY)
    t = buy(day, sid)
    st = run_to(day, sid, START + 10 * DAY)
    assert st["finished"] and st["day"]["trade"] is None
    b = st["day"]["session"]
    assert (b["end_flag"], b["end_flag_label"], b["end_detail"]) == (ENDED_BY_TIMEOUT, "Time limit reached", "end of the replay")
    tr = day.trades(sid)["trades"][0]
    assert tr["id"] == t["id"] and tr["status"] == "closed" and tr["exit_type"] == "session timeout"
    assert tr["exit_ms"] == st["cursor_ms"] == st["data_end_ms"] and tr["exit_price"] == st["price"]
    assert tr["exit_note"] == "Session ended: end of the replay." and tr["timeout_rule"] == TIMEOUT_RULE
    assert tr["outcome"] in ("WIN", "LOSS", "BREAKEVEN")
    assert tr["after"]["complete"] is False and tr["after"]["high"] is None        # no candle after the last one
    assert st["cash"] == pytest.approx(tr["cash_after_exit"])
    assert day.state(sid)["day"]["session"]["ended_at"] == b["ended_at"]           # ended once, not again on every read


def test_the_end_of_the_replay_without_a_trade(db: Path):
    day, _, sid = setup(db, end=START + DAY)
    st = run_to(day, sid, START + 10 * DAY)
    b = st["day"]["session"]
    assert (b["end_flag"], b["trades_taken"]) == (ENDED_BY_TIMEOUT, 0) and day.trades(sid)["trades"] == []


def test_stop_by_the_user_closes_the_trade_and_lets_the_twin_run(db: Path):
    hit = FIRST_CURSOR + 10 * Q
    day, strat, sid = setup(db, {hit: (PRICE, PRICE + 11.0, PRICE - 1.0, PRICE)})   # later the original target is reached
    t = buy(day, sid)
    day.step(sid, 2)
    st = strat.stop(sid)
    b = st["day"]["session"]
    assert (b["end_flag"], b["end_flag_label"], b["end_detail"], b["can_stop"]) == (ENDED_BY_USER, "Stopped by the user", "stopped by the user", False)
    assert st["cursor_ms"] == C2 and st["day"]["trade"] is None and st["day"]["entries_allowed"] is False
    tr = day.trades(sid)["trades"][0]
    assert tr["exit_type"] == "session stopped" and tr["exit_ms"] == C2 and tr["exit_price"] == quarter_candle(C2)[3]
    assert tr["exit_note"] == "Session ended: stopped by the user." and tr["timeout_rule"] == TIMEOUT_RULE
    # the session is over, so the twin and the 24 hours after the exit are read beyond the cursor
    tw = day.auto_trades(sid)["trades"][0]
    assert tw["trade_id"] == t["id"] and tw["status"] == "closed" and tw["exit_type"] == "take-profit" and tw["exit_ms"] == hit
    assert tr["after"]["complete"] is True and tr["after"]["high"] == PRICE + 11.0 and tr["after_high_ms"] == hit
    with pytest.raises(Conflict, match="already ended"):
        strat.stop(sid)


def test_stop_without_an_open_trade(db: Path):
    day, strat, sid = setup(db)
    st = strat.stop(sid)
    assert st["day"]["session"]["end_flag"] == ENDED_BY_USER and day.trades(sid)["trades"] == []


def test_manual_clock_counts_real_time_and_times_out(db: Path):
    day, strat, sid = setup(db, run_mode="manual", time_limit_hours=0.05)           # 3 minutes
    buy(day, sid)
    b = strat.add_clock(sid, 100)["day"]["session"]
    assert (b["clock_seconds"], b["clock_left_seconds"], b["ended"]) == (100.0, 80.0, False)
    for bad in (0, -1, 121, float("inf"), True):
        with pytest.raises(Invalid, match="seconds must be"):
            strat.add_clock(sid, bad)
    st = strat.add_clock(sid, 80)
    b = st["day"]["session"]
    assert (b["clock_seconds"], b["clock_left_seconds"]) == (180.0, 0.0)
    assert (b["end_flag"], b["end_detail"]) == (ENDED_BY_TIMEOUT, "time limit of 0.05 hours")
    tr = day.trades(sid)["trades"][0]
    assert tr["exit_type"] == "session timeout" and tr["exit_ms"] == FIRST_CURSOR and tr["run_mode"] == "manual"
    assert strat.add_clock(sid, 60)["day"]["session"]["clock_seconds"] == 180.0     # an ended session's clock stands still


def test_manual_without_a_time_limit_never_times_out_and_automatic_has_no_clock(db: Path):
    day, strat, sid = setup(db, run_mode="manual", time_limit_hours=0)
    for _ in range(40):
        b = strat.add_clock(sid, 120)["day"]["session"]
    assert (b["clock_seconds"], b["clock_left_seconds"], b["ended"]) == (4800.0, None, False)
    _, strat2, auto = setup(db.with_name("auto.db"))
    with pytest.raises(Conflict, match="Only a Manual session"):
        strat2.add_clock(auto, 5)


def test_reset_keeps_the_setup_and_clears_the_end(db: Path):
    day, strat, sid = setup(db, run_mode="manual", max_trades=7, time_limit_hours=1)
    buy(day, sid)
    strat.add_clock(sid, 60)
    strat.stop(sid)
    st = day.reset(sid)
    b = st["day"]["session"]
    assert (b["run_mode"], b["max_trades"], b["time_limit_hours"], b["strategy"]["key"]) == ("manual", 7, 1.0, "rline_retest_confirmed")
    assert (b["ended"], b["end_flag"], b["trades_taken"], b["clock_seconds"]) == (False, None, 0, 0.0)
    assert st["day"]["entries_allowed"] is True and st["cursor_ms"] == FIRST_CURSOR


# ---------------------------------------------------------------------- API
SYMBOL = "LINKUSDT"   # a coin no other API test module downloads


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


@pytest.fixture(scope="module")
def client() -> TestClient:
    main.store.client = FineFakeClient(NOW)   # no network during tests
    return TestClient(main.app)


def test_api_setup_create_stop_and_clock(client: TestClient):
    o = client.get("/api/strategy/setup").json()
    assert [s["label"] for s in o["strategies"]] == ["RLine_Retest confirmed v1", "RLine_Retest signal candle v1"]
    assert all(s["timeframe"] == "1h" and s["rules"] for s in o["strategies"])
    body = {"symbol": SYMBOL, "start": iso(START), "end": iso(END), "run_mode": "manual",
            "strategy": "rline_retest_signal", "max_trades": 4, "time_limit_hours": 1, "pictures": False}
    r = client.post("/api/strategy/sessions", json=body)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    try:
        st = client.get(f"/api/day/sessions/{sid}").json()   # TestClient ran the background prepare already
        assert st["status"] == "ready" and st["mode"] == "day"
        b = st["day"]["session"]
        assert (b["run_mode"], b["strategy"]["key"], b["strategy"]["timeframe"], b["max_trades"], b["pictures"]) == (
            "manual", "rline_retest_signal", "1h", 4, False)
        r = client.post(f"/api/strategy/sessions/{sid}/clock", json={"seconds": 30})
        assert r.status_code == 200 and r.json()["day"]["session"]["clock_seconds"] == 30.0
        assert client.post(f"/api/strategy/sessions/{sid}/clock", json={"seconds": 500}).status_code == 422
        r = client.post(f"/api/strategy/sessions/{sid}/stop")
        assert r.status_code == 200 and r.json()["day"]["session"]["end_flag"] == ENDED_BY_USER
        r = client.post(f"/api/strategy/sessions/{sid}/stop")
        assert r.status_code == 409 and "already ended" in r.json()["detail"]
    finally:
        client.delete(f"/api/sessions/{sid}")   # leave nothing behind for the other API test modules


@pytest.mark.parametrize("change, status", [
    ({"run_mode": "both"}, 422), ({"strategy": "nope"}, 422), ({"max_trades": 0}, 422),
    ({"time_limit_hours": -1}, 422), ({"run_mode": "automatic", "time_limit_hours": 3}, 422), ({"pictures": "yes"}, 422),
])
def test_api_create_rejects_a_bad_setup(client: TestClient, change: dict, status: int):
    body = {"symbol": SYMBOL, "start": iso(START), "end": iso(END), "run_mode": "manual",
            "strategy": "rline_retest_confirmed", "max_trades": 4, **change}
    before = len(client.get("/api/sessions").json())
    assert client.post("/api/strategy/sessions", json=body).status_code == status
    assert len(client.get("/api/sessions").json()) == before


def test_api_stop_on_a_plain_day_session_is_refused(client: TestClient):
    r = client.post("/api/day/sessions", json={"symbol": SYMBOL, "start": iso(START), "end": iso(END)})
    sid = r.json()["id"]
    try:
        assert client.get(f"/api/day/sessions/{sid}").json()["day"]["session"] is None
        assert client.post(f"/api/strategy/sessions/{sid}/stop").status_code == 409
    finally:
        client.delete(f"/api/sessions/{sid}")
