"""Automatic mode: a strategy session's plug-in trades the replay by itself (strat_run.py).

The two RLine_Retest plug-ins must trade exactly like the autorun's CTest rules they were taken
from, so the first tests run both side by side on the autorun tests' scripted candles. Then the
session limits, the pictures, a strategy the engine has never seen, and the HTTP routes."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.replay.autorun import Autorun
from app.replay.day_engine import DayEngine
from app.replay.engine import Conflict, Invalid
from app.replay.strat import ENDED_BY_TIMEOUT, ENDED_BY_TRADE_LIMIT, ENDED_BY_USER, StrategySessions
from app.replay.strat_run import EngineMarket, StrategyRun
from app.strategies import plugin
from app.strategies.plugin import Decision, Levels, TradingStrategy
from app.strategies.plugins._rline_retest import RLineRetest
from tests.fakes import DAY, H, Q, FineFakeClient
from tests.test_autorun import D1, D3, D4, OVERRIDES, P, sig, signals
from tests.test_autorun import run_all as autorun_all
from tests.test_day import END, NOW, PNG, START, make

CONFIRMED, SIGNAL = "rline_retest_confirmed", "rline_retest_signal"
SIGNAL_COLS = ("signal_ms", "bar_ms", "zone_top", "zone_bottom", "entry_level", "rules", "touches", "ema_above",
               "outcome", "pattern", "entry", "stop", "take_profit", "stop_dist_pct", "atr", "est_fees", "fee_share")
TRADE_COLS = ("entry_ms", "entry_price", "stop_used", "take_profit", "coins", "position_value", "buy_fee",
              "zone_upper", "zone_lower", "exit_ms", "exit_price", "exit_type", "exit_reason", "pnl", "r_multiple")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "replay.db"


def setup(db: Path, monkeypatch, fake: list[dict] | None, overrides: dict | None = None, strategy: str = CONFIRMED,
          end: int = END, max_trades: int = 100, pictures: bool = False) -> tuple[DayEngine, StrategySessions, StrategyRun, str]:
    """A prepared Automatic session whose RLine_Retest plug-in finds the scripted signals `fake`."""
    day = make(db, OVERRIDES if overrides is None else overrides)
    strat = StrategySessions(day)
    if fake is not None:
        monkeypatch.setattr(RLineRetest, "find_signals", lambda self, candles: fake)
    sid = strat.create("BTCUSDT", START, end, run_mode="automatic", strategy=strategy, max_trades=max_trades, pictures=pictures)["id"]
    day.prepare(sid)
    return day, strat, StrategyRun(strat), sid


def run_all(day: DayEngine, runner: StrategyRun, sid: str, pictures: bool = False, max_steps: int = 400) -> list[dict]:
    """Advance until the run is done, standing in for the browser: it takes the trade pictures asked for."""
    calls = []
    for _ in range(2000):
        st = runner.advance(sid, max_steps)
        calls.append(st)
        assert st["run"]["need"] in (None, "trade_picture")
        if st["run"]["need"]:
            assert pictures, "a picture was asked for with screenshots off"
            day.attach_image(sid, st["day"]["picture_due"]["id"], PNG)
        if st["run"]["phase"] == "done":
            return calls
    raise AssertionError("the run did not finish")


def same(a: dict, b: dict, cols: tuple[str, ...]) -> None:
    for c in cols:
        assert a[c] == pytest.approx(b[c]), f"{c}: {a[c]!r} != {b[c]!r}"


# ---------------------------------------------------- the same as the autorun
def test_confirmed_strategy_trades_like_the_autorun_it_came_from(db: Path, tmp_path: Path, monkeypatch):
    old = make(tmp_path / "old.db", OVERRIDES)
    old_sid = old.prepare(old.create_session("BTCUSDT", START, END, start_balance=100_000)["id"])["id"]
    monkeypatch.setattr(old, "_strategy_signals", lambda s, js, p: signals())
    ar = Autorun(old)
    ar.start(old_sid, {"pictures": False})               # the defaults: CTest__01's rules
    autorun_all(old, ar, old_sid)

    day, strat, runner, sid = setup(db, monkeypatch, signals())
    calls = run_all(day, runner, sid)
    rows, old_rows = runner.signals(sid), ar.signals(old_sid)
    assert [r["outcome"] for r in rows] == [r["outcome"] for r in old_rows] and len(rows) == 6
    for r, o in zip(rows, old_rows):
        same(r, o, SIGNAL_COLS)
    trades, old_trades = day.trades(sid)["trades"], old.trades(old_sid)["trades"]
    assert len(trades) == len(old_trades) >= 1
    for t, o in zip(trades, old_trades):
        if o["status"] == "closed":
            same(t, o, TRADE_COLS)
        else:   # the autorun leaves a trade open at the end of the data; the session closes it there
            same(t, o, TRADE_COLS[:9])
            assert t["exit_type"] == "session timeout"

    # the first trade is the hammer two candles after the D1 signal, out at its 2R target
    tr = trades[0]
    assert (tr["entry_ms"], tr["exit_ms"], tr["exit_type"]) == (D1 + 2 * Q, D1 + 8 * Q, "take-profit")
    assert (tr["run_mode"], tr["strategy_key"], tr["strategy_version"], tr["timeframe"]) == ("automatic", CONFIRMED, "1", "1h")
    assert tr["notes"].startswith("Automatic (RLine_Retest confirmed v1): A hammer at the zone.") and "Target: 2 R." in tr["notes"]
    assert rows[0]["trade_id"] == tr["id"] and rows[0]["strategy_key"] == CONFIRMED and rows[0]["confirm_ms"] == D1 + 2 * Q
    fee_row = next(r for r in rows if r["outcome"] == "fees_too_high")
    assert fee_row["outcome_label"] == "The fees are too high for the risk" and fee_row["details"]["fee_share"] >= 0.2
    assert fee_row["trade_id"] is None and fee_row["details"]["levels"] == "atr"

    # no signal is left: the run goes to the replay's last candle, the session's time limit
    st = calls[-1]
    b = st["day"]["session"]
    assert st["finished"] and (b["end_flag"], b["end_detail"]) == (ENDED_BY_TIMEOUT, "end of the replay")
    assert b["run"]["done"] and not b["run"]["can_run"] and b["run"]["signals"] == 6
    assert {o["key"]: o["count"] for o in b["run"]["outcomes"]} == {k: sum(r["outcome"] == k for r in rows) for k in {r["outcome"] for r in rows}}
    assert any(e["kind"] == "strategy_buy" and e["n"] == 1 and e["code"] == "hammer" for c in calls for e in c["events"])
    assert day.zones(sid) == []                           # the closed trades' zones are gone, the trades keep the edges
    assert tr["zone_upper"] == pytest.approx(P(D1) + 0.5)


def test_signal_candle_strategy_trades_like_the_autorun_it_came_from(db: Path, tmp_path: Path, monkeypatch):
    """CTest__02's rules: the signal candle's close, only above the EMA 200, out at a close below the zone."""
    brk = D3 + 5 * Q                     # a close below the D3 zone, with the low above the hard stop
    over = {brk: (P(D3 + 4 * Q), P(D3 + 4 * Q) + 0.1, P(D3) - 2.2, P(D3) - 2.1)}
    fake = lambda: [sig(D1, P(D1) + 0.5, P(D1) - 2.0), sig(D3, P(D3) + 0.5, P(D3) - 2.0)]  # noqa: E731
    trend = {D1: 0, D3: 1}               # D1: daily close below the EMA 200, D3: above

    def with_trend(engine: DayEngine) -> None:
        real = engine._daily_ema
        monkeypatch.setattr(engine, "_daily_ema", lambda s: {**real(s), "ema_above": trend.get(s["cursor_ms"], real(s)["ema_above"])})

    old = make(tmp_path / "old.db", over)
    old_sid = old.prepare(old.create_session("BTCUSDT", START, END, start_balance=100_000)["id"])["id"]
    monkeypatch.setattr(old, "_strategy_signals", lambda s, js, p: fake())
    with_trend(old)
    ar = Autorun(old)
    ar.start(old_sid, {"entry": "signal", "trend_filter": "above", "break_exit": True, "ratio": 2.5, "pictures": False})
    autorun_all(old, ar, old_sid)

    day, strat, runner, sid = setup(db, monkeypatch, fake(), overrides=over, strategy=SIGNAL)
    with_trend(day)
    run_all(day, runner, sid)
    rows, old_rows = runner.signals(sid), ar.signals(old_sid)
    assert [r["outcome"] for r in rows] == ["trend_filter", "traded"]
    for r, o in zip(rows, old_rows):
        same(r, o, SIGNAL_COLS)
    assert rows[0]["outcome_label"] == "The daily close is not above the EMA 200" and rows[0]["confirm_ms"] is None
    (tr,), (o,) = day.trades(sid)["trades"], old.trades(old_sid)["trades"]
    same(tr, o, TRADE_COLS)
    assert (tr["entry_ms"], tr["exit_ms"], tr["exit_type"], tr["exit_reason"]) == (D3, brk, "early exit", "setup_broken")
    assert tr["exit_note"] == "Automatic (RLine_Retest signal candle v1): A candle closed below the zone."
    assert tr["exit_price"] > tr["stop_used"] and day.zones(sid) == []


def test_a_restart_in_the_middle_changes_nothing(db: Path, tmp_path: Path, monkeypatch):
    day, strat, runner, sid = setup(db, monkeypatch, signals())
    run_all(day, runner, sid)
    day2, strat2, _, sid2 = setup(tmp_path / "two.db", monkeypatch, signals())
    for _ in range(2000):                 # three candles per call, every call with a runner that remembers nothing
        st = StrategyRun(strat2).advance(sid2, 3)
        if st["run"]["phase"] == "done":
            break
    one, two = StrategyRun(strat).signals(sid), StrategyRun(strat2).signals(sid2)
    assert [r["outcome"] for r in one] == [r["outcome"] for r in two]
    for a, b in zip(day.trades(sid)["trades"], day2.trades(sid2)["trades"]):
        same(a, b, TRADE_COLS)


# -------------------------------------------------------------- the limits
def test_trade_limit_ends_the_run(db: Path, monkeypatch):
    fake = [sig(D1, P(D1) + 0.5, P(D1) - 2.0), sig(D3 + 3 * H, P(D3) + 0.5, P(D3) - 2.0)]
    day, strat, runner, sid = setup(db, monkeypatch, fake, max_trades=1)
    before = day.state(sid)["day"]["session"]["run"]
    assert (before["started"], before["phase"], before["phase_label"], before["can_run"]) == (False, "new", "Not started", True)
    st = run_all(day, runner, sid)[-1]
    b = st["day"]["session"]
    assert (b["end_flag"], b["end_detail"], b["trades_taken"]) == (ENDED_BY_TRADE_LIMIT, "1 trade taken", 1)
    assert st["cursor_ms"] == D1 + 8 * Q                  # screenshots off: the replay stays on the exit candle
    assert len(runner.signals(sid)) == 1 and b["run"]["phase_label"] == "Finished"
    assert st["day"]["exit_choice"] is False and st["day"]["phase"] == "trading"   # no "end the day?" question
    assert runner.advance(sid)["run"]["phase"] == "done"  # a finished run stays finished


def test_trade_open_at_the_end_of_the_replay_is_closed_there(db: Path, monkeypatch):
    fake = [sig(D4 + 2 * H, P(D4) + 0.5, P(D4) - 2.0)]   # an engulfing at +8, the target is never reached
    day, strat, runner, sid = setup(db, monkeypatch, fake)
    st = run_all(day, runner, sid)[-1]
    (tr,) = day.trades(sid)["trades"]
    assert tr["exit_type"] == "session timeout" and tr["exit_ms"] == st["data_end_ms"] == st["cursor_ms"]
    assert st["day"]["session"]["end_flag"] == ENDED_BY_TIMEOUT and runner.signals(sid)[0]["outcome"] == "traded"


def test_stop_during_an_entry_window_closes_the_signal(db: Path, monkeypatch):
    day, strat, runner, sid = setup(db, monkeypatch, [sig(D3, P(D3) + 0.5, P(D3) - 2.0)])
    st = runner.advance(sid)                              # the jump to the signal: the window starts
    assert st["cursor_ms"] == D3 and st["run"]["phase"] == "waiting" and st["run"]["window_candle"] == 0
    assert [e["kind"] for e in st["events"]] == ["jump"] and st["events"][0]["reason"].startswith("RLine_Retest confirmed: ")
    assert len(day.zones(sid)) == 1                       # the signal's zone is on the chart
    st = runner.advance(sid, 2)
    assert st["cursor_ms"] == D3 + 2 * Q and st["run"]["window_candle"] == 2
    strat.stop(sid)
    st = runner.advance(sid)
    assert st["run"]["phase"] == "done" and st["day"]["session"]["end_flag"] == ENDED_BY_USER
    (row,) = runner.signals(sid)
    assert (row["outcome"], row["outcome_label"]) == ("session_ended", "The session ended during the entry window")


def test_a_session_stopped_before_its_start_has_nothing_to_run(db: Path, monkeypatch):
    day, strat, runner, sid = setup(db, monkeypatch, signals())
    assert strat.stop(sid)["day"]["session"]["run"]["can_run"] is False
    assert runner.advance(sid)["run"]["phase"] == "done" and runner.signals(sid) == []


def test_reset_clears_the_run_and_the_same_run_comes_again(db: Path, monkeypatch):
    day, strat, runner, sid = setup(db, monkeypatch, signals())
    run_all(day, runner, sid)
    first = [(t["entry_ms"], t["exit_ms"], t["pnl"]) for t in day.trades(sid)["trades"]]
    st = day.reset(sid)
    assert st["day"]["session"]["run"]["started"] is False and st["day"]["session"]["ended"] is False
    assert runner.signals(sid) == [] and day.trades(sid)["trades"] == []
    run_all(day, runner, sid)
    assert [(t["entry_ms"], t["exit_ms"], t["pnl"]) for t in day.trades(sid)["trades"]] == first


# ------------------------------------------------------------- the pictures
def test_trade_pictures_hold_the_run_and_no_day_picture_is_asked_for(db: Path, monkeypatch):
    fake = [sig(D1, P(D1) + 0.5, P(D1) - 2.0), sig(D3, P(D3) + 0.5, P(D3) - 2.0)]
    day, strat, runner, sid = setup(db, monkeypatch, fake, pictures=True)
    for _ in range(200):
        st = runner.advance(sid)
        if st["run"]["need"]:
            break
    else:
        raise AssertionError("no trade picture asked for")
    held, due = st["cursor_ms"], st["day"]["picture_due"]["id"]
    assert st["run"]["need"] == "trade_picture" and held + Q == START + 2 * DAY   # 24:00 of the D1 trade's exit day
    for _ in range(3):                                    # nobody takes it: the run does not move on
        st = runner.advance(sid)
        assert st["run"]["need"] == "trade_picture" and st["cursor_ms"] == held
    day.attach_image(sid, due, PNG)
    run_all(day, runner, sid, pictures=True)
    trades = day.trades(sid)["trades"]
    assert len(trades) == 2 and all(t["has_image"] for t in trades)
    assert not any(d["has_image"] for d in day.days(sid)["days"])   # trade pictures only


def test_the_last_trades_picture_is_taken_after_the_session_ended(db: Path, monkeypatch):
    day, strat, runner, sid = setup(db, monkeypatch, [sig(D1, P(D1) + 0.5, P(D1) - 2.0)], max_trades=1, pictures=True)
    for _ in range(200):
        st = runner.advance(sid)
        if st["run"]["need"]:
            break
    b = st["day"]["session"]
    # the session ended at the exit candle; the replay went on to that day's 24:00 for the picture
    assert b["end_flag"] == ENDED_BY_TRADE_LIMIT and b["ended_local"].endswith(" 08:15") and b["run"]["phase"] == "finishing"
    assert st["cursor_ms"] + Q == START + 2 * DAY and b["run"]["can_run"]
    day.attach_image(sid, st["day"]["picture_due"]["id"], PNG)
    st = runner.advance(sid)
    assert st["run"]["phase"] == "done" and st["run"]["need"] is None and st["cursor_ms"] + Q == START + 2 * DAY


def test_at_the_replays_last_candle_the_picture_is_due_at_once(db: Path, monkeypatch):
    end = END - 6 * H                                     # the replay ends at 18:00: the exit day's 24:00 never comes
    day, strat, runner, sid = setup(db, monkeypatch, [sig(D4 + 2 * H, P(D4) + 0.5, P(D4) - 2.0)], end=end, pictures=True)
    for _ in range(200):
        st = runner.advance(sid)
        if st["run"]["need"]:
            break
    assert st["finished"] and st["cursor_ms"] + Q < START + 6 * DAY and st["day"]["session"]["end_flag"] == ENDED_BY_TIMEOUT
    assert st["day"]["picture_due"]["exit_type"] == "session timeout"
    day.attach_image(sid, st["day"]["picture_due"]["id"], PNG)
    assert runner.advance(sid)["run"]["phase"] == "done"


def test_screenshots_off_means_no_picture_is_ever_due(db: Path, monkeypatch):
    day, strat, runner, sid = setup(db, monkeypatch, [sig(D1, P(D1) + 0.5, P(D1) - 2.0), sig(D3, P(D3) + 0.5, P(D3) - 2.0)])
    calls = run_all(day, runner, sid)
    assert all(c["day"]["picture_due"] is None for c in calls) and not any(e["kind"] == "picture" for c in calls for e in c["events"])
    assert len(day.trades(sid)["trades"]) == 2 and not any(t["has_image"] for t in day.trades(sid)["trades"])


# ------------------------------------------- a strategy the engine never saw
class Morning(TradingStrategy):
    """Buy the close of every 06:00 candle; stop 1 % below, target 2 % above. No zone, no filter."""
    key, name, version, timeframe = "test_morning", "Test morning", "7", "15m"
    description = "A strategy that exists only in the tests."

    def settings(self) -> dict:
        return {"stop_pct": 1.0, "target_pct": 2.0}

    def find_signals(self, candles):
        return [{"bar_ms": int(t) * 1000} for t in candles["time"] if (int(t) * 1000 - START) % DAY == 6 * H and int(t) * 1000 >= START]

    def levels(self, signal, entry_price, market):
        assert isinstance(market, EngineMarket) and market.candles("15m", 4)["close"].iloc[-1] == entry_price
        return Levels(stop=entry_price * 0.99, take_profit=entry_price * 1.02, kind="percent", text="Stop 1 % below, target 2 % above.")


class NeverEnters(Morning):
    key, name = "test_never", "Test never"

    def entry(self, signal, n, cur, prev):
        return Decision.wait()


@pytest.fixture
def extra_strategies(monkeypatch):
    monkeypatch.setattr(plugin, "_registry", {**plugin.trading_strategies(), "test_morning": Morning(), "test_never": NeverEnters()})


def test_a_new_strategy_runs_without_any_engine_change(db: Path, monkeypatch, extra_strategies):
    day, strat, runner, sid = setup(db, monkeypatch, None, overrides={}, strategy="test_morning", max_trades=2)
    st = run_all(day, runner, sid)[-1]
    trades = day.trades(sid)["trades"]
    assert 1 <= len(trades) <= 2 and all(t["status"] == "closed" for t in trades)
    for t in trades:
        assert (t["strategy_name"], t["strategy_version"], t["timeframe"], t["run_mode"]) == ("Test morning", "7", "15m", "automatic")
        assert (t["entry_ms"] - START) % DAY == 6 * H
        assert t["stop_used"] == pytest.approx(t["entry_price"] * 0.99) and t["take_profit"] == pytest.approx(t["entry_price"] * 1.02)
        # no zone of its own: the trade's band runs from its stop to its entry
        assert (t["zone_upper"], t["zone_lower"]) == (pytest.approx(t["entry_price"]), pytest.approx(t["stop_used"]))
    rows = runner.signals(sid)
    assert rows[0]["outcome"] == "traded" and rows[0]["pattern"] == "signal" and rows[0]["details"]["levels"] == "percent"
    assert rows[0]["zone_top"] == rows[0]["zone_bottom"] == pytest.approx(trades[0]["entry_price"])   # no band to log
    assert st["day"]["session"]["end_flag"] in (ENDED_BY_TRADE_LIMIT, ENDED_BY_TIMEOUT)
    assert st["day"]["session"]["strategy"]["settings"] == {"stop_pct": 1.0, "target_pct": 2.0}


def test_a_window_that_never_enters_ends_with_the_replay(db: Path, monkeypatch, extra_strategies):
    day, strat, runner, sid = setup(db, monkeypatch, None, overrides={}, strategy="test_never")
    st = run_all(day, runner, sid)[-1]
    rows = runner.signals(sid)
    assert rows[0]["outcome"] == "end_of_data" and rows[0]["signal_ms"] == START + 6 * H   # the first signal waited to the end
    # the later ones were passed during its window; the log still holds every one of them
    assert [r["outcome"] for r in rows[1:]] == ["waiting"] * (len(rows) - 1) and len(rows) >= 5
    assert day.trades(sid)["trades"] == [] and st["day"]["session"]["end_flag"] == ENDED_BY_TIMEOUT


def test_a_changed_plugin_does_not_run_an_old_session(db: Path, monkeypatch, extra_strategies):
    day, strat, runner, sid = setup(db, monkeypatch, None, overrides={}, strategy="test_morning")
    monkeypatch.setattr(Morning, "version", "8")
    with pytest.raises(Conflict, match="has changed since this session was set up"):
        runner.advance(sid)
    monkeypatch.setattr(Morning, "version", "7")
    monkeypatch.setattr(Morning, "settings", lambda self: {"stop_pct": 2.0, "target_pct": 2.0})   # a rule changed, the version did not
    with pytest.raises(Conflict, match="has changed"):
        runner.advance(sid)
    monkeypatch.setattr(plugin, "_registry", {k: v for k, v in plugin.trading_strategies().items() if k != "test_morning"})
    with pytest.raises(Conflict, match="not registered any more"):
        runner.advance(sid)


# ------------------------------------------------------------------ guards
def test_only_a_strategy_session_runs(db: Path, monkeypatch):
    """(What a Manual session takes by hand: tests/test_strat_manual.py.)"""
    day, strat, runner, sid = setup(db, monkeypatch, signals())
    plain = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(plain)
    with pytest.raises(Conflict, match="not a strategy session"):
        runner.advance(plain)
    with pytest.raises(Conflict, match="strategy session"):
        Autorun(day).start(sid, {})                       # the old autorun stays out of strategy sessions
    for action in ("buy", "step", "sell", "adjust", "leg", "jump", "back"):
        with pytest.raises(Conflict, match="Automatic session"):
            strat.manual_guard(sid, action)
        strat.manual_guard(plain, action), strat.manual_guard("no-such-session", action)
    with pytest.raises(Conflict, match="Only a Manual session"):
        runner.take(sid, 5)
    with pytest.raises(Conflict, match="Only a Manual session"):
        runner.skip(sid)
    run = day.state(sid)["day"]["session"]["run"]
    assert run["proposal"] is None and run["exit_notice"] is None


def test_jump_to_goes_forward_only(db: Path, monkeypatch):
    day, strat, runner, sid = setup(db, monkeypatch, [])
    st = day.jump_to(sid, D1 + Q, "Because.")
    assert st["cursor_ms"] == D1 + Q and st["events"][0]["reason"] == "Because." and st["day"]["can_step_back"] is False
    days = {d["date"]: d for d in day.days(sid)["days"]}
    assert days["2018-05-10"]["left_how"] == "skip"   # the day the jump left
    for bad in (D1, D1 + Q, END + DAY, 1.5, True):
        with pytest.raises(Invalid):
            day.jump_to(sid, bad, "x")
    z = day.add_zone(sid, "1h", P(D1) + 0.5, P(D1) - 0.5)
    day.buy(sid, z["id"], take_profit=P(D1 + Q) + 50.0, stop=P(D1 + Q) - 50.0)
    with pytest.raises(Conflict, match="A trade is open"):
        day.jump_to(sid, D3, "x")


# --------------------------------------------------------------------- API
SYMBOL = "ZECUSDT"   # a coin no other API test module downloads


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


@pytest.fixture(scope="module")
def client() -> TestClient:
    main.store.client = FineFakeClient(NOW)   # no network during tests
    return TestClient(main.app)


def test_api_run_signals_and_the_manual_buttons_are_refused(client: TestClient, monkeypatch):
    monkeypatch.setattr(RLineRetest, "find_signals", lambda self, candles: [])
    body = {"symbol": SYMBOL, "start": iso(START), "end": iso(END), "run_mode": "automatic", "strategy": CONFIRMED, "max_trades": 4, "pictures": False}
    sid = client.post("/api/strategy/sessions", json=body).json()["id"]
    try:
        day_api, strat_api = f"/api/day/sessions/{sid}", f"/api/strategy/sessions/{sid}"
        for path, payload in (("buy", {"zone_id": 1, "take_profit": 1.0, "confidence": 5}), ("sell", {"reason": "news"}), ("step", {"n": 1}),
                              ("step-back", {}), ("skip", None), ("goto", {"date": "2018-05-12"}), ("next-ema-day", None),
                              ("next-signal", {}), ("end-day", None), ("keep-trading", None), ("continue", None),
                              ("scale", {"side": "sell", "reason": "stalls"})):
            r = client.post(f"{day_api}/{path}", json=payload) if payload is not None else client.post(f"{day_api}/{path}")
            assert r.status_code == 409 and "Automatic session" in r.json()["detail"], path
        assert client.patch(f"{day_api}/trade", json={"stop": 1.0}).status_code == 409
        assert client.post(f"/api/sessions/{sid}/step", json={"interval": "15m", "n": 1}).status_code == 409
        assert client.post(f"{day_api}/autorun", json={"rules": {}}).status_code == 409
        assert client.get(f"{day_api}").json()["cursor_ms"] == START           # nothing moved
        assert client.post(f"{strat_api}/take", json={"confidence": 5}).status_code == 409
        assert client.post(f"{strat_api}/skip").status_code == 409

        assert client.post(f"{strat_api}/run", json={"max_steps": 0}).status_code == 422
        st = client.post(f"{strat_api}/run", json={}).json()   # no signal at all: straight to the last candle
        assert st["finished"] and st["run"]["phase"] == "done" and st["run"]["need"] is None
        assert st["day"]["session"]["end_flag"] == ENDED_BY_TIMEOUT and st["events"][0]["kind"] == "jump"
        assert client.get(f"{strat_api}/signals").json() == []
        assert client.post(f"{day_api}/reset").status_code == 200              # a reset is still allowed
    finally:
        client.delete(f"/api/sessions/{sid}")
