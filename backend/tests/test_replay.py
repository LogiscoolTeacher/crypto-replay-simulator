"""Replay engine tests against the fake exchange (no network)."""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from app.data.candle_store import CandleStore
from app.indicators import core
from app.replay.engine import Conflict, Invalid, NotFound, ReplayEngine
from app.replay.session_store import SessionStore
from tests.fakes import DAY, H, LISTING, FakeClient, hourly_price

NOW = LISTING + 400 * H + 1  # 400 closed hourly candles exist (LISTING .. LISTING+399H)
START = LISTING + 100 * H
END = LISTING + 300 * H


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "replay.db"


@pytest.fixture
def engine(db: Path) -> ReplayEngine:
    return ReplayEngine(CandleStore(db, FakeClient(NOW)), SessionStore(db))


@pytest.fixture
def session(engine: ReplayEngine) -> dict:
    s = engine.create_session("BTCUSDT", START, END, base_interval="1h", start_balance=10_000, fee_rate=0.001)
    assert s["status"] == "preparing" and s["cursor_ms"] is None
    return engine.prepare(s["id"])


def close_at(t_ms: int) -> float:
    return hourly_price(t_ms) + 0.5


# -------------------------------------------------------------------- sessions
def test_prepare_downloads_all_view_intervals_and_sets_cursor(engine: ReplayEngine, session: dict):
    assert session["status"] == "ready"
    assert session["cursor_ms"] == START and session["data_start_ms"] == START and session["data_end_ms"] == END
    assert session["price"] == close_at(START)
    assert session["finished"] is False
    stats = {(r["symbol"], r["interval"]): r for r in engine.candles.stats()}
    assert {"1h", "4h", "1D"} <= {iv for _, iv in stats}
    assert all(iv not in ("1m", "5m", "15m") for _, iv in stats)  # nothing finer than the base
    assert stats[("BTCUSDT", "1h")]["first_ms"] == LISTING  # warm-up clamped to the listing


def test_create_session_validation(engine: ReplayEngine):
    with pytest.raises(Invalid):
        engine.create_session("FOO", START, END)
    with pytest.raises(Invalid):
        engine.create_session("BTCUSDT", START, END, base_interval="2h")
    with pytest.raises(Invalid):
        engine.create_session("BTCUSDT", END, START)
    with pytest.raises(Invalid):
        engine.create_session("BTCUSDT", START, END, start_balance=0)
    with pytest.raises(Invalid):
        engine.create_session("BTCUSDT", START, END, fee_rate=0.5)
    with pytest.raises(NotFound):
        engine.state("nope")


def test_prepare_fails_cleanly_without_data(engine: ReplayEngine):
    s = engine.create_session("BTCUSDT", LISTING - 50 * H, LISTING - 10 * H)
    with pytest.raises(Invalid):
        engine.prepare(s["id"])
    st = engine.state(s["id"])
    assert st["status"] == "error" and "no 1h candles" in st["error"]
    with pytest.raises(Conflict):
        engine.step(s["id"])


def test_start_moves_to_the_200th_day_after_the_listing(db: Path):
    """Chosen start before the coin had 200 daily candles: the replay begins on day 200, the
    automatic name follows, the note explains it, and the daily EMA/SMA 200 exist from the start."""
    now = LISTING + 400 * DAY + 1
    eng = ReplayEngine(CandleStore(db, FakeClient(now)), SessionStore(db))
    s = eng.prepare(eng.create_session("BTCUSDT", LISTING - 10 * DAY, LISTING + 300 * DAY)["id"])
    day200 = LISTING + 200 * DAY
    assert s["data_start_ms"] == s["cursor_ms"] == day200 and s["start_ms"] == LISTING - 10 * DAY
    assert s["name"] == f"BTCUSDT 1h from {_day(day200)}"
    assert s["start_note"].startswith(f"Start moved from {_day(LISTING - 10 * DAY)} to {_day(day200)}")
    assert _day(LISTING) in s["start_note"] and "200 daily candles" in s["start_note"]
    g = eng.view(s["id"], "1h", 50)["gauges"]
    assert g["ema_long"]["value"] is not None and "sma_long" not in g
    assert eng.state(s["id"])["start_note"] == s["start_note"]
    # Reset goes back to the moved start, not the chosen date.
    eng.step(s["id"]); eng.step(s["id"])
    assert eng.reset(s["id"])["cursor_ms"] == day200
    # An own name stays; a start late enough is left alone and carries no note.
    s2 = eng.prepare(eng.create_session("BTCUSDT", LISTING + 10 * DAY, LISTING + 300 * DAY, name="My run")["id"])
    assert s2["name"] == "My run" and s2["data_start_ms"] == day200 and s2["start_note"]
    s3 = eng.prepare(eng.create_session("BTCUSDT", LISTING + 250 * DAY, LISTING + 300 * DAY)["id"])
    assert s3["data_start_ms"] == LISTING + 250 * DAY and s3["start_note"] is None


def test_young_coin_starts_where_asked_with_a_warning(engine: ReplayEngine, session: dict):
    """Fewer than 200 days of data exist at all: the start stays, the note says how many daily
    candles there are and from when the 200-day averages will appear."""
    assert session["data_start_ms"] == START  # LISTING + 100 h: 4 closed days before it
    note = session["start_note"]
    assert note.startswith("BTCUSDT has only 4 daily candles before the start")
    assert _day(LISTING) in note and note.endswith(f"appear from {_day(LISTING + 200 * DAY)}.")
    one = engine.prepare(engine.create_session("BTCUSDT", LISTING + 25 * H, END)["id"])
    assert "only 1 daily candle before" in one["start_note"]
    none = engine.prepare(engine.create_session("BTCUSDT", LISTING, END)["id"])
    assert none["start_note"].startswith("BTCUSDT has no daily candle before the start")


def test_sell_in_parts(engine: ReplayEngine, session: dict):
    """25 % / 50 % / all of what is left: every part is its own closed row with the bucket's number
    and a part number, carrying its share of the coins, cost and buy fee; the bucket stays open
    with the rest and closes as the last part. Journal rows know the part; numbering continues."""
    sid = session["id"]
    b = engine.buy(sid, 1000)
    engine.step(sid, n=3)
    p1 = engine.sell(sid, b["id"], part=0.25)
    assert (p1["seq"], p1["part"], p1["part_pct"], p1["status"], p1["parent_id"]) == (1, 1, 25.0, "closed", b["id"])
    assert p1["qty"] == pytest.approx(b["qty"] / 4) and p1["cost"] == pytest.approx(250) and p1["buy_fee"] == pytest.approx(0.25)
    assert p1["realized_pnl"] == pytest.approx(p1["proceeds"] - p1["sell_fee"] - 250) and p1["entry_price"] == b["entry_price"]
    assert p1["opened_ms"] == b["opened_ms"] and p1["buy_rsi"] == b["buy_rsi"] and p1["held_high"] is not None
    st = engine.state(sid)
    o = st["buckets"][0]
    assert o["id"] == b["id"] and o["qty"] == pytest.approx(b["qty"] * 0.75) and o["cost"] == pytest.approx(750) and o["buy_fee"] == pytest.approx(0.75)
    assert o["parts_sold"] == 1 and st["account"]["closed_buckets"] == 1 and st["account"]["cash"] == pytest.approx(9000 + p1["proceeds"] - p1["sell_fee"])
    assert st["reference"]["last_sell"]["seq"] == 1 and st["reference"]["last_sell"]["part"] == 1
    p2 = engine.sell(sid, b["id"], part=0.5)   # half of what is left, not of the original bucket
    assert (p2["part"], p2["part_pct"]) == (2, 50.0) and p2["qty"] == pytest.approx(b["qty"] * 0.375) and p2["cost"] == pytest.approx(375)
    p3 = engine.sell(sid, b["id"])               # the rest closes the bucket itself
    assert p3["id"] == b["id"] and (p3["part"], p3["part_pct"], p3["status"]) == (3, 100.0, "closed") and p3["qty"] == pytest.approx(b["qty"] * 0.375)
    with pytest.raises(Conflict):
        engine.sell(sid, b["id"], part=0.5)
    closed = [x for x in engine.trades(sid)["buckets"] if x["status"] == "closed"]
    assert [(x["seq"], x["part"]) for x in closed] == [(1, 1), (1, 2), (1, 3)]
    assert sum(x["qty"] for x in closed) == pytest.approx(b["qty"]) and sum(x["cost"] for x in closed) == pytest.approx(1000)
    assert sum(x["buy_fee"] for x in closed) == pytest.approx(1.0)
    assert engine.state(sid)["account"]["fees_paid"] == pytest.approx(1.0 + sum(x["sell_fee"] for x in closed))
    assert [t["side"] for t in engine.trades(sid)["trades"]] == ["buy", "sell", "sell", "sell"]
    rows = engine.snapshots(sid)
    assert [(r["side"], r["readings"]["bucket_seq"], r["readings"]["part"]) for r in rows] == [("buy", 1, None), ("sell", 1, 1), ("sell", 1, 2), ("sell", 1, 3)]
    assert all(r["readings"]["at_buy"]["quote"] == 1000 for r in rows[1:]) and rows[1]["readings"]["part_pct"] == 25.0
    assert rows[1]["readings"]["realized_pnl_pct"] == pytest.approx(p1["realized_pnl"] / 250 * 100)
    assert "part" in engine.snapshots_csv(sid).splitlines()[0].split(",")
    nb = engine.buy(sid, 100)
    assert nb["seq"] == 2  # parts do not use up bucket numbers
    for bad in (0, 1.5, float("nan")):
        with pytest.raises(Invalid):
            engine.sell(sid, nb["id"], part=bad)
    # Whole coins: a part is rounded down to whole coins; none left means refused.
    w = engine.buy(sid, 500, coins=3)
    with pytest.raises(Invalid):
        engine.sell(sid, w["id"], part=0.25, whole=True)
    assert engine.sell(sid, w["id"], part=0.5, whole=True)["qty"] == 1.0
    assert engine.state(sid)["buckets"][-1]["qty"] == 2.0
    assert engine.sell(sid, w["id"], part=1, whole=True)["qty"] == 2.0  # all is never rounded


def test_reasons_are_saved_and_counted(engine: ReplayEngine, session: dict):
    """The ticked reasons, the note and the checklist version go into the journal row of every
    buy and sell; a sell's copy of the buy readings carries the buy reasons. The statistics
    count closed trades per buy reason (ticked against not ticked) and sells per sell reason,
    measured by P&L percent, and flag groups under 10 as too few to judge."""
    sid = session["id"]
    with pytest.raises(Invalid):
        engine.buy(sid, 500, reasons=["at_resistance"])  # a sell reason on a buy
    with pytest.raises(Invalid):
        engine.buy(sid, 500, reasons=["at_support"], note="x" * 501)
    assert engine.state(sid)["buckets"] == []  # refused before anything changed
    b = engine.buy(sid, 1000, reasons=["at_support", "rsi_low", "at_support"], note="  bounce off the tube  ")
    rows = engine.snapshots(sid)
    assert rows[-1]["readings"]["reasons"] == ["at_support", "rsi_low"] and rows[-1]["readings"]["reason_note"] == "bounce off the tube"
    assert rows[-1]["readings"]["checklist"] == "v1"
    engine.step(sid, n=3)
    p1 = engine.sell(sid, b["id"], part=0.5, reasons=["trim"])
    p2 = engine.sell(sid, b["id"], reasons=["profit_target", "at_resistance"], note="")
    rows = engine.snapshots(sid)
    assert [r["readings"]["reasons"] for r in rows[1:]] == [["trim"], ["profit_target", "at_resistance"]]
    assert rows[-1]["readings"]["reason_note"] is None and rows[-1]["readings"]["at_buy"]["reasons"] == ["at_support", "rsi_low"]
    head, *body = engine.snapshots_csv(sid).splitlines()
    cols = head.split(",")
    assert "reasons" in cols and "at_buy_reasons" in cols and "checklist" in cols
    assert "profit_target | at_resistance" in body[-1]
    # A bucket bought without the checklist still records the version, with nothing ticked.
    plain = engine.buy(sid, 100)
    assert engine.snapshots(sid)[-1]["readings"]["reasons"] == [] and engine.snapshots(sid)[-1]["readings"]["checklist"] == "v1"
    st = engine.reason_stats()
    assert st["checklist"] == "v1" and st["min_group"] == 10 and st["symbols"] == ["BTCUSDT"] and st["unlisted"] == 0
    assert st["trades"] == 1 and st["sells"] == 2  # the open bucket is not a closed trade yet
    trade_pct = (p1["realized_pnl"] + p2["realized_pnl"]) / 1000 * 100
    assert st["trades_avg_pct"] == pytest.approx(trade_pct)
    buy = {r["key"]: r for r in st["buy"]}
    assert set(buy) == {"at_support", "rsi_low", "above_ema200", "lower_bb", "fib_level", "volume_support", "bounce_candle", "rsi_div_up", "breakout", "big_drop", "other"}
    assert buy["at_support"]["ticked"] == {"count": 1, "avg_pct": pytest.approx(trade_pct), "few": True}
    assert buy["at_support"]["unticked"] == {"count": 0, "avg_pct": None, "few": True}
    assert buy["breakout"]["ticked"]["count"] == 0 and buy["breakout"]["unticked"]["count"] == 1
    sell = {r["key"]: r for r in st["sell"]}
    assert set(sell) == {"at_resistance", "rsi_high", "upper_bb", "profit_target", "trim", "weak_volume", "rsi_div_down", "rejection_candle", "support_broken", "below_ema200", "other"}
    assert sell["trim"]["ticked"] == {"count": 1, "avg_pct": pytest.approx(p1["realized_pnl"] / p1["cost"] * 100), "few": True}
    assert sell["at_resistance"]["ticked"]["count"] == 1 and sell["rsi_high"]["ticked"]["count"] == 0
    # Closing the plain bucket makes a second trade; the coin filter and the "few" flag follow.
    engine.sell(sid, plain["id"], reasons=["other"], note="just because")
    st = engine.reason_stats("BTCUSDT")
    assert st["trades"] == 2 and st["sells"] == 3 and {r["key"]: r for r in st["buy"]}["at_support"]["unticked"]["count"] == 1
    assert engine.reason_stats("ETHUSDT")["trades"] == 0 and engine.reason_stats("ETHUSDT")["symbols"] == ["BTCUSDT"]
    # Journal rows from before the checklist existed are left out and counted as unlisted.
    with engine.sessions.tx() as conn:
        snap = engine.sessions.get_snapshot(conn, sid, plain["id"], "buy")
        import json
        rd = json.loads(snap["readings"])
        for k in ("reasons", "reason_note", "checklist"):
            rd.pop(k)
        engine.sessions.update_snapshot(conn, snap["id"], readings=json.dumps(rd))
    st = engine.reason_stats()
    assert st["trades"] == 1 and st["sells"] == 3 and st["unlisted"] == 1


def test_target_and_stop(engine: ReplayEngine, session: dict):
    """Target and stop are set at the buy and stored with the bucket and the journal. They do
    nothing until the bucket is armed; a manual sell still records whether they were reached.
    Armed, the first base candle whose high reaches the target (low reaches the stop) sells
    everything left at that level, or at the candle's open when it gapped beyond it; a candle
    reaching both is the stop. The sell happens at that candle's time, inside the step."""
    from app.replay.engine import _iso
    sid = session["id"]
    for bad in (dict(tp_pct=0), dict(tp_pct=1001), dict(sl_pct=100), dict(sl_pct=-1), dict(sl_pct=float("nan"))):
        with pytest.raises(Invalid):
            engine.buy(sid, 500, **bad)
    # Fake prices cycle: hourly price 100 + hour % 50 (close = price + 0.5, high = price + 1,
    # low = price - 1). Step to the start of a cycle so the close is 100.5 and rises 1 per hour.
    phase = (session["cursor_ms"] // H) % 50
    if phase:
        engine.step(sid, n=50 - phase)
    cursor0 = engine.state(sid)["cursor_ms"]
    b1 = engine.buy(sid, 1000, tp_pct=3, sl_pct=2)
    p0 = b1["entry_price"]
    assert p0 == 100.5
    assert b1["tp_price"] == pytest.approx(p0 * 1.03) and b1["sl_price"] == pytest.approx(p0 * 0.98)
    assert (b1["tp_pct"], b1["sl_pct"], b1["armed_ms"], b1["trigger"]) == (3, 2, None, None)
    assert b1["tp_dist_pct"] == pytest.approx(3) and b1["sl_dist_pct"] == pytest.approx(-2)
    rd = engine.snapshots(sid)[-1]["readings"]
    assert rd["tp_pct"] == 3 and rd["tp_price"] == pytest.approx(p0 * 1.03) and rd["sl_price"] == pytest.approx(p0 * 0.98)
    plain = engine.buy(sid, 100)
    assert plain["tp_price"] is None and engine.snapshots(sid)[-1]["readings"]["tp_pct"] is None
    with pytest.raises(Invalid):
        engine.arm(sid, plain["id"], True)   # nothing to arm
    with pytest.raises(NotFound):
        engine.arm(sid, 99999, True)
    # Not armed: the target (103.515) is passed by the high of the 3rd hour (104) but nothing
    # sells; the manual sell records that it was reached.
    st = engine.step(sid, n=5)
    assert st["auto_sells"] == [] and len(st["buckets"]) == 2
    sold = engine.sell(sid, b1["id"], reasons=["profit_target"])
    assert sold["trigger"] is None and sold["realized_pnl"] > 0
    rd = engine.snapshots(sid)[-1]["readings"]
    assert rd["trigger"] is None and rd["armed_at"] is None
    assert rd["target_hit"] is True and rd["target_hit_at"] == _iso(cursor0 + 3 * H)
    assert rd["stop_hit"] is False and rd["stop_hit_at"] is None
    assert rd["at_buy"]["tp_pct"] == 3
    # Armed: the target fires on the first candle whose high reaches it, at the level price,
    # with the cursor on that candle; the step then moves on.
    cursor = engine.state(sid)["cursor_ms"]
    b2 = engine.buy(sid, 1000, tp_pct=3, sl_pct=2)   # entry 105.5, target 108.665: the high of hour +3 is 109
    armed = engine.arm(sid, b2["id"], True)
    assert armed["armed_ms"] == cursor and armed["armed_iso"] is not None
    assert engine.arm(sid, b2["id"], False)["armed_ms"] is None
    engine.arm(sid, b2["id"], True)
    st = engine.step(sid, n=10)
    assert st["cursor_ms"] == cursor + 10 * H and [x["id"] for x in st["auto_sells"]] == [b2["id"]]
    auto = st["auto_sells"][0]
    assert auto["trigger"] == "target" and auto["status"] == "closed" and auto["closed_ms"] == cursor + 3 * H
    assert auto["exit_price"] == pytest.approx(b2["tp_price"]) and auto["armed_ms"] == cursor
    assert [x["id"] for x in st["buckets"]] == [plain["id"]]
    rd = engine.snapshots(sid)[-1]["readings"]
    assert rd["trigger"] == "target" and rd["reasons"] == [] and rd["checklist"] == "v1" and rd["armed_at"] == armed["armed_iso"]
    assert rd["target_hit"] is True and rd["stop_hit"] is False and rd["cash_after"] == pytest.approx(engine.state(sid)["account"]["cash"])
    assert engine.snapshots(sid)[-1]["time_ms"] == cursor + 3 * H
    trades = engine.trades(sid)["trades"]
    assert trades[-1]["time_ms"] == cursor + 3 * H and trades[-1]["price"] == pytest.approx(b2["tp_price"])
    # A stop with a gap: the price drops from 149 to 100 at hour 50 of the cycle; that candle
    # opens far below the stop, so the fill is the open, not the stop price.
    engine.step(sid, n=33)   # hour 48 of the cycle: close 148.5
    cursor = engine.state(sid)["cursor_ms"]
    assert (cursor // H) % 50 == 48
    b3 = engine.buy(sid, 500, tp_pct=50, sl_pct=2)
    engine.arm(sid, b3["id"], True)
    st = engine.step(sid, interval="1D", n=1)
    auto = st["auto_sells"][0]
    assert auto["id"] == b3["id"] and auto["trigger"] == "stop" and auto["closed_ms"] == cursor + 2 * H
    assert auto["exit_price"] == pytest.approx(100.0) and auto["exit_price"] < b3["sl_price"]
    assert engine.snapshots(sid)[-1]["readings"]["stop_hit"] is True and engine.snapshots(sid)[-1]["readings"]["target_hit"] is False
    # Both levels inside one candle: the stop wins. Parts: the auto sell takes what is left.
    if (engine.state(sid)["cursor_ms"] // H) % 50 == 49:
        engine.step(sid, n=1)   # not right before the drop
    b4 = engine.buy(sid, 400, tp_pct=0.3, sl_pct=0.3)   # the next candle's range (price-1 .. price+2) covers both
    engine.sell(sid, b4["id"], part=0.5)
    engine.arm(sid, b4["id"], True)
    st = engine.step(sid, n=1)
    auto = st["auto_sells"][0]
    assert auto["id"] == b4["id"] and auto["trigger"] == "stop" and auto["part"] == 2 and auto["qty"] == pytest.approx(b4["qty"] / 2)
    assert auto["exit_price"] == pytest.approx(b4["sl_price"])
    with pytest.raises(Conflict):
        engine.arm(sid, b4["id"], True)
    assert engine.reset(sid)["buckets"] == []


def test_performance_report(engine: ReplayEngine, session: dict):
    """The strategy-tester report: numbers over the closed rows (a part is a trade), the equity
    curve at every base candle with open buckets at market, the drawdown with peak and trough,
    one row per trade and the monthly returns; and the report over all sessions."""
    sid = session["id"]
    empty = engine.performance(sid)
    assert empty["scope"] == "session" and empty["summary"]["trades"] == 0 and empty["summary"]["win_rate_pct"] is None
    assert empty["curve"]["value"][0] == 10_000 and len(empty["curve"]["time"]) == empty["curve"]["points"] == 2  # start + the cursor candle
    assert empty["summary"]["max_drawdown_pct"] == 0 and empty["trades"] == [] and len(empty["months"]) == 1
    phase = (session["cursor_ms"] // H) % 50
    if phase:
        engine.step(sid, n=50 - phase)
    cursor0 = engine.state(sid)["cursor_ms"]
    b = engine.buy(sid, 1000)                      # 9.9403 coins at 100.5, fee 1
    engine.step(sid, n=3)                          # close 103.5
    p1 = engine.sell(sid, b["id"], part=0.5)       # a part: its own trade
    engine.step(sid, n=2)                          # close 105.5
    p2 = engine.sell(sid, b["id"])
    c = engine.buy(sid, 500)                       # stays open
    rep = engine.performance(sid)
    sm = rep["summary"]
    assert sm["trades"] == 2 and sm["winners"] == 2 and sm["losers"] == 0 and sm["win_rate_pct"] == 100
    assert sm["net_pnl"] == pytest.approx(p1["realized_pnl"] + p2["realized_pnl"]) and sm["net_pnl_pct"] == pytest.approx(sm["net_pnl"] / 100)
    assert sm["gross_loss"] == 0 and sm["profit_factor"] == "inf" and sm["avg_pnl"] == pytest.approx(sm["net_pnl"] / 2)
    assert sm["best"]["label"] == "#1.2" and sm["worst"]["label"] == "#1.1" and sm["best"]["pct"] == pytest.approx(p2["realized_pnl_pct"])
    assert sm["avg_held_ms"] == pytest.approx(4 * H) and sm["max_held_ms"] == 5 * H and sm["win_streak"] == 2 and sm["loss_streak"] == 0
    assert sm["open_count"] == 1 and sm["open_pnl"] == pytest.approx(-0.5) and sm["fees"] == pytest.approx(engine.state(sid)["account"]["fees_paid"])
    assert sm["equity"] == pytest.approx(engine.state(sid)["account"]["equity"]) and sm["equity_pct"] == pytest.approx((sm["equity"] / 10_000 - 1) * 100)
    assert [t["label"] for t in rep["trades"]] == ["#1.1", "#1.2"] and rep["trades"][0]["held_ms"] == 3 * H and rep["trades"][1]["trigger"] is None
    cv = rep["curve"]
    assert cv["kind"] == "equity" and len(cv["time"]) == len(cv["value"]) == len(cv["drawdown_pct"]) == cv["points"]
    assert cv["time"][0] == session["cursor_ms"] // 1000 and cv["value"][0] == 10_000 and cv["time"][-1] == engine.state(sid)["cursor_ms"] // 1000
    i0 = cv["time"].index(cursor0 // 1000)
    assert cv["value"][i0] == pytest.approx(9000 + b["qty"] * 100.5)        # the buy's fee shows at its candle
    assert cv["value"][i0 + 1] == pytest.approx(9000 + b["qty"] * 101.5)    # marked to market while held
    assert cv["value"][-1] == pytest.approx(sm["equity"]) and all(d <= 0 for d in cv["drawdown_pct"])
    assert cv["value"][cv["peak_index"]] >= cv["value"][cv["trough_index"]] and cv["peak_index"] <= cv["trough_index"]
    assert sm["max_drawdown_pct"] == pytest.approx(-min(cv["drawdown_pct"])) and sm["equity_high"] == max(cv["value"])
    assert sm["peak"]["equity"] == cv["value"][cv["peak_index"]] and sm["trough"]["iso"] is not None
    assert len(rep["months"]) >= 1 and sum(m["pnl"] for m in rep["months"]) == pytest.approx(sm["equity"] - 10_000)
    # All sessions: a second session on another day adds its closed trades; the curve is the
    # realized P&L added up trade by trade.
    s2 = engine.prepare(engine.create_session("BTCUSDT", LISTING + 10 * H, LISTING + 60 * H, base_interval="1h", start_balance=5_000, fee_rate=0.001)["id"])
    d = engine.buy(s2["id"], 1000)
    engine.step(s2["id"], n=1)
    loss = engine.sell(s2["id"], d["id"]) if engine.state(s2["id"])["price"] < d["entry_price"] else None
    allrep = engine.performance_all()
    assert allrep["scope"] == "all" and allrep["sessions"] == 2 and allrep["start_balance"] == 15_000
    assert allrep["summary"]["trades"] == 2 + (1 if loss else 0)
    assert allrep["curve"]["kind"] == "cumulative" and allrep["curve"]["points"] == allrep["summary"]["trades"] + 1 and allrep["curve"]["value"][0] == 0
    assert allrep["curve"]["value"][-1] == pytest.approx(allrep["summary"]["net_pnl"])
    assert allrep["summary"]["net_pnl_pct"] == pytest.approx(allrep["summary"]["net_pnl"] / 150)
    assert sum(m["pnl"] for m in allrep["months"]) == pytest.approx(allrep["summary"]["net_pnl"])
    assert {t["symbol"] for t in allrep["trades"]} == {"BTCUSDT"} and allrep["summary"]["open_count"] == 1 + (0 if loss else 1)


def test_actions_blocked_until_ready(engine: ReplayEngine):
    s = engine.create_session("BTCUSDT", START, END)
    for fn in (lambda: engine.step(s["id"]), lambda: engine.buy(s["id"], 100), lambda: engine.view(s["id"])):
        with pytest.raises(Conflict):
            fn()


def test_sessions_persist_across_engine_instances(db: Path, engine: ReplayEngine, session: dict):
    engine.buy(session["id"], 1000)
    other = ReplayEngine(CandleStore(db, FakeClient(NOW)), SessionStore(db))
    st = other.state(session["id"])
    assert st["cash"] == 9000 and len(st["buckets"]) == 1
    assert [x["id"] for x in other.list_sessions()] == [session["id"]]
    other.delete_session(session["id"])
    with pytest.raises(NotFound):
        engine.state(session["id"])
    with pytest.raises(NotFound):
        engine.delete_session(session["id"])


# -------------------------------------------------------------------- stepping
def test_step_base_interval(engine: ReplayEngine, session: dict):
    sid = session["id"]
    st = engine.step(sid)
    assert st["cursor_ms"] == START + H and st["price"] == close_at(START + H)
    st = engine.step(sid, n=5)
    assert st["cursor_ms"] == START + 6 * H


def test_step_coarser_interval_completes_current_candle(engine: ReplayEngine, session: dict):
    sid = session["id"]
    # START = LISTING + 100h is 4h-aligned. Cursor at START means the 4h candle
    # [START, START+4h) has 1 of 4 hours: stepping 4h must move to its last hour.
    st = engine.step(sid, interval="4h")
    assert st["cursor_ms"] == START + 3 * H
    st = engine.step(sid, interval="4h")  # a 4h candle just closed: advance a full candle
    assert st["cursor_ms"] == START + 7 * H
    engine.step(sid)  # now at +8h, first hour of a new 4h candle
    st = engine.step(sid, interval="1D")
    day_end = ((START + 8 * H) // DAY + 1) * DAY - H
    assert st["cursor_ms"] == day_end
    with pytest.raises(Invalid):
        engine.step(sid, interval="15m")
    with pytest.raises(Invalid):
        engine.step(sid, n=0)


def test_step_clamps_at_end_and_marks_finished(engine: ReplayEngine, session: dict):
    sid = session["id"]
    st = engine.step(sid, n=10_000)
    assert st["cursor_ms"] == END and st["finished"] is True
    st = engine.step(sid)  # no-op at the end
    assert st["cursor_ms"] == END
    st = engine.step(sid, interval="1D")
    assert st["cursor_ms"] == END


def test_step_skips_gaps(db: Path):
    hole = START + 2 * H
    engine = ReplayEngine(CandleStore(db, FakeClient(NOW, holes={hole})), SessionStore(db))
    s = engine.prepare(engine.create_session("BTCUSDT", START, END)["id"])
    engine.step(s["id"])
    st = engine.step(s["id"])
    assert st["cursor_ms"] == START + 3 * H  # jumped over the missing hour


# --------------------------------------------------------------------- trading
def test_buy_and_sell_math(engine: ReplayEngine, session: dict):
    sid = session["id"]
    p0 = close_at(START)
    b = engine.buy(sid, 1000)
    assert b["seq"] == 1 and b["status"] == "open" and b["entry_price"] == p0
    assert b["buy_fee"] == pytest.approx(1.0)
    assert b["qty"] == pytest.approx(999 / p0)
    assert b["unrealized_pnl"] == pytest.approx(-1.0)  # only the fee so far
    st = engine.state(sid)
    assert st["cash"] == 9000
    assert st["account"]["equity"] == pytest.approx(9999.0)
    assert st["account"]["invested"] == 1000 and st["account"]["fees_paid"] == pytest.approx(1.0)

    b2 = engine.buy(sid, 250.5)
    assert b2["seq"] == 2 and engine.state(sid)["cash"] == pytest.approx(9000 - 250.5)

    engine.step(sid, n=10)
    p1 = close_at(START + 10 * H)
    st = engine.state(sid)
    assert st["buckets"][0]["market_value"] == pytest.approx(999 / p0 * p1)
    assert st["buckets"][0]["unrealized_pnl"] == pytest.approx(999 / p0 * p1 - 1000)

    closed = engine.sell(sid, b["id"])
    proceeds = 999 / p0 * p1
    assert closed["status"] == "closed" and closed["exit_price"] == p1
    assert closed["sell_fee"] == pytest.approx(proceeds * 0.001)
    assert closed["realized_pnl"] == pytest.approx(proceeds * 0.999 - 1000)
    assert closed["realized_pnl_pct"] == pytest.approx((proceeds * 0.999 - 1000) / 10)
    st = engine.state(sid)
    assert st["cash"] == pytest.approx(9000 - 250.5 + proceeds * 0.999)
    assert len(st["buckets"]) == 1 and st["buckets"][0]["seq"] == 2
    assert st["account"]["closed_buckets"] == 1 and st["account"]["realized_pnl"] == pytest.approx(closed["realized_pnl"])
    # Reference prices for "if I buy now": latest buy (#2, same time but higher seq) and latest sell (#1).
    ref = st["reference"]
    assert ref["last_buy"]["seq"] == 2 and ref["last_buy"]["price"] == p0
    assert ref["last_sell"]["seq"] == 1 and ref["last_sell"]["price"] == p1
    assert ref["last_sell"]["basis"] == "price" and ref["last_sell"]["pct"] == pytest.approx(0.0)
    assert ref["last_closed"]["seq"] == 1 and ref["last_closed"]["status"] == "closed"
    assert st["account"]["equity"] == pytest.approx(st["cash"] + st["buckets"][0]["market_value"])

    journal = engine.trades(sid)
    assert [t["side"] for t in journal["trades"]] == ["buy", "buy", "sell"]
    assert journal["trades"][-1]["cash_after"] == pytest.approx(st["cash"])
    assert len(journal["buckets"]) == 2


def test_trading_validation(engine: ReplayEngine, session: dict):
    sid = session["id"]
    with pytest.raises(Invalid):
        engine.buy(sid, 0)
    with pytest.raises(Invalid):
        engine.buy(sid, 0.5)
    with pytest.raises(Invalid):
        engine.buy(sid, 10_000.01)
    engine.buy(sid, 10_000)  # all-in is allowed
    assert engine.state(sid)["cash"] == 0
    with pytest.raises(Invalid):
        engine.buy(sid, 1)
    b = engine.state(sid)["buckets"][0]
    engine.sell(sid, b["id"])
    with pytest.raises(Conflict):
        engine.sell(sid, b["id"])
    with pytest.raises(NotFound):
        engine.sell(sid, 9999)
    other = engine.prepare(engine.create_session("ETHUSDT", START, END)["id"])
    with pytest.raises(NotFound):  # bucket belongs to another session
        engine.sell(other["id"], b["id"])

def test_trades_record_rsi_of_the_chosen_timeframe(engine: ReplayEngine, session: dict):
    """RSI(14) at buy/sell equals the value on the last bar of a view of that timeframe."""
    sid = session["id"]
    engine.step(sid, n=7)
    b = engine.buy(sid, 300, interval="4h")
    v = engine.view(sid, "4h", limit=1, indicators=[("rsi", {"period": 14})])
    assert v["partial_last"] in (True, False)
    assert b["buy_rsi"] == pytest.approx(v["indicators"][0]["outputs"]["rsi"][-1])
    assert b["buy_rsi_interval"] == "4h" and b["sell_rsi"] is None
    engine.step(sid, n=9)
    closed = engine.sell(sid, b["id"])  # default: base timeframe
    v = engine.view(sid, "1h", limit=1, indicators=[("rsi", {"period": 14})])
    assert closed["sell_rsi"] == pytest.approx(v["indicators"][0]["outputs"]["rsi"][-1])
    assert closed["sell_rsi_interval"] == "1h" and closed["buy_rsi"] == b["buy_rsi"]
    assert 0.0 <= closed["sell_rsi"] <= 100.0
    with pytest.raises(Invalid):
        engine.buy(sid, 100, interval="15m")  # finer than the base timeframe
    with pytest.raises(Invalid):
        engine.buy(sid, 100, interval="bogus")


def test_old_database_gets_rsi_columns(tmp_path: Path):
    import sqlite3
    from app.replay.session_store import SCHEMA, SessionStore
    old = tmp_path / "old.db"
    legacy = SCHEMA
    for col in ("buy_rsi", "buy_rsi_interval", "sell_rsi", "sell_rsi_interval"):
        legacy = "\n".join(line for line in legacy.splitlines() if not line.strip().startswith(col))
    legacy = legacy.replace("realized_pnl REAL,", "realized_pnl REAL")
    conn = sqlite3.connect(old)
    conn.executescript(legacy)
    conn.close()
    SessionStore(old)
    cols = {r[1] for r in sqlite3.connect(old).execute("PRAGMA table_info(buckets)")}
    assert {"buy_rsi", "buy_rsi_interval", "sell_rsi", "sell_rsi_interval"} <= cols


def test_view_gauges_match_the_indicators(engine: ReplayEngine, session: dict):
    """The side-panel RSI equals the RSI line's last point; EMA 200 equals the 1D EMA line's last point."""
    sid = session["id"]
    engine.step(sid, n=5)
    v = engine.view(sid, "4h", limit=3, indicators=[("rsi", {"period": 14})])
    g = v["gauges"]
    assert g["rsi"]["interval"] == "4h" and g["rsi"]["period"] == 14
    assert g["rsi"]["value"] == pytest.approx(v["indicators"][0]["outputs"]["rsi"][-1])
    assert g["price"] == v["close"][-1]
    assert g["ema_long"]["period"] == 200 and g["ema_long"]["interval"] == "1D"
    assert "sma_long" not in g   # the daily SMA 200 is gone: the EMA 200 is the one long average
    b = engine.view(sid, "4h", limit=3, indicators=[("bbands", {"period": 20, "mult": 2.0})])["indicators"][0]["outputs"]
    bb = g["bb"]
    assert bb["period"] == 20 and bb["mult"] == 2.0 and bb["interval"] == "4h"
    if b["upper"][-1] is None:
        assert bb["pctb"] is None and bb["width_pct"] is None
    else:
        assert bb["upper"] == pytest.approx(b["upper"][-1]) and bb["lower"] == pytest.approx(b["lower"][-1])
        assert bb["pctb"] == pytest.approx((g["price"] - b["lower"][-1]) / (b["upper"][-1] - b["lower"][-1]))
        assert bb["width_pct"] == pytest.approx((b["upper"][-1] - b["lower"][-1]) / b["middle"][-1] * 100)
    d = engine.view(sid, "1D", limit=3, indicators=[("ema", {"period": 200})])
    ema_line = d["indicators"][0]["outputs"]["ema"][-1]
    assert g["ema_long"]["interval"] == "1D" and g["ema_long"]["period"] == 200
    if ema_line is None:  # the synthetic listing is too young for 200 daily bars
        assert g["ema_long"]["value"] is None and g["ema_long"]["price_pct"] is None
    else:
        assert g["ema_long"]["value"] == pytest.approx(ema_line)
        assert g["ema_long"]["price_pct"] == pytest.approx((g["price"] / ema_line - 1) * 100)


def test_fib_levels_and_validation(engine: ReplayEngine, session: dict):
    sid = session["id"]
    engine.step(sid, n=10)
    cursor = engine.state(sid)["cursor_ms"]
    f = engine.add_fib(sid, "4h", START, 100.0, cursor, 150.0)
    assert f["direction"] == "up" and f["interval"] == "4h"
    assert [lv["ratio"] for lv in f["levels"]] == [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    assert f["levels"][0]["price"] == 150.0 and f["levels"][-1]["price"] == 100.0  # 0 % at the swing end
    assert f["levels"][4]["price"] == pytest.approx(150 - 50 * 0.618)
    down = engine.add_fib(sid, None, START, 150.0, cursor, 100.0)
    assert down["direction"] == "down" and down["levels"][4]["price"] == pytest.approx(100 + 50 * 0.618)
    assert [x["id"] for x in engine.fibs(sid)] == [f["id"], down["id"]]
    for bad in (
        dict(interval="15m", t1_ms=START, p1=100.0, t2_ms=cursor, p2=150.0),   # finer than base
        dict(interval=None, t1_ms=START, p1=100.0, t2_ms=cursor + H, p2=150.0),  # after the cursor
        dict(interval=None, t1_ms=START, p1=100.0, t2_ms=cursor, p2=100.0),      # no height
        dict(interval=None, t1_ms=START, p1=float("nan"), t2_ms=cursor, p2=100.0),
        dict(interval=None, t1_ms=-10_000_000_000, p1=100.0, t2_ms=cursor, p2=150.0),
    ):
        with pytest.raises(Invalid):
            engine.add_fib(sid, **bad)
    engine.delete_fib(sid, f["id"])
    with pytest.raises(NotFound):
        engine.delete_fib(sid, f["id"])
    assert [x["id"] for x in engine.fibs(sid)] == [down["id"]]
    engine.reset(sid)
    assert engine.fibs(sid) == []  # drawings would otherwise point into the future


def test_auto_fib_follows_the_extremes_of_the_lookback(engine: ReplayEngine, session: dict):
    sid = session["id"]
    engine.step(sid, n=40)
    v = engine.view(sid, "1h", limit=60, auto_fib=30)
    a = v["auto_fib"]
    highs = list(zip(v["high"][-30:], v["time"][-30:]))
    lows = list(zip(v["low"][-30:], v["time"][-30:]))
    hi_p, hi_t = max(highs, key=lambda x: (x[0], -x[1]))
    lo_p, lo_t = min(lows, key=lambda x: (x[0], x[1]))
    assert a["bars"] == 30 and a["lookback"] == 30
    if lo_t <= hi_t:
        assert a["direction"] == "up" and (a["p1"], a["p2"]) == (lo_p, hi_p) and (a["t1_ms"], a["t2_ms"]) == (lo_t * 1000, hi_t * 1000)
    else:
        assert a["direction"] == "down" and (a["p1"], a["p2"]) == (hi_p, lo_p)
    assert a["levels"][0]["price"] == a["p2"] and a["levels"][-1]["price"] == a["p1"]
    assert engine.view(sid, "1h", limit=5)["auto_fib"] is None
    with pytest.raises(Invalid):
        engine.view(sid, "1h", limit=5, auto_fib=1)


def test_support_resistance_levels():
    from app.replay.engine import support_resistance
    # Flat daily bars (high 100, low 95) with planted swings: ~110 is touched three times, ~130 twice,
    # 90 and 80 once each.
    rows = []
    for i in range(60):
        hi, lo = 100.0, 95.0
        if i in (10, 30): hi = {10: 130.0, 30: 130.5}[i]
        if i in (20, 40, 50): hi = {20: 110.0, 40: 111.0, 50: 110.5}[i]
        if i in (15, 45): lo = {15: 80.0, 45: 90.0}[i]
        rows.append((i * 86400, 98.0, hi, lo, 98.0))
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    lv = support_resistance(df, 99.0, tol_pct=1.5)
    res, sup = lv["resistance"], lv["support"]
    assert lv["swings"] == 7
    assert [l["label"] for l in res] == ["R1", "R2"] and [l["label"] for l in sup] == ["S1", "S2"]
    assert res[0]["touches"] == 3 and res[0]["price"] == pytest.approx((110 + 111 + 110.5) / 3)  # grouped within 1.5 %
    assert res[1]["touches"] == 2 and res[1]["price"] == pytest.approx(130.25) and res[1]["score"] > 3  # window high bonus
    assert sup[0]["price"] == 90.0 and sup[1]["price"] == 80.0 and sup[1]["score"] > sup[0]["score"]  # nearest first, low bonus
    assert res[0]["first_ms"] == 20 * 86400 * 1000 and res[0]["last_ms"] == 50 * 86400 * 1000 and res[0]["pct"] > 0
    assert support_resistance(df.head(5), 99.0) == {"resistance": [], "support": [], "swings": 0}
    with pytest.raises(Invalid):
        support_resistance(df, 99.0, tol_pct=0)
    # Only three per side, the strongest ones.
    # Peaks every 20 days at four heights (110, 120, 130, 140): four levels, three survive.
    many = pd.DataFrame([(i * 86400, 98.0, 100.0 + (10 * (1 + (i // 20) % 4) if i % 20 == 10 else 0), 95.0, 98.0) for i in range(300)],
                        columns=["time", "open", "high", "low", "close"])
    top = support_resistance(many, 99.0)["resistance"]
    assert [l["label"] for l in top] == ["R1", "R2", "R3"] and len({round(l["price"]) for l in top}) == 3


def test_btc_context_uses_bitcoin_days_known_at_cursor(tmp_path: Path):
    """An ETH session still gets Bitcoin candles: two years of closed days plus the forming day to the cursor."""
    db = tmp_path / "ctx.db"
    now = LISTING + 800 * DAY + 1
    client = FakeClient(now)
    eng = ReplayEngine(CandleStore(db, client), SessionStore(db))
    s = eng.prepare(eng.create_session("ETHUSDT", LISTING + 780 * DAY, LISTING + 790 * DAY)["id"])
    sid = s["id"]
    eng.step(sid, n=30)  # 30 hours: 6 hours into the second day
    cursor = eng.state(sid)["cursor_ms"]
    assert cursor == LISTING + 781 * DAY + 6 * H
    ctx = eng.btc_context(sid, years=2)
    assert ctx["symbol"] == "BTCUSDT" and ctx["interval"] == "1D" and ctx["years"] == 2
    assert ctx["count"] == 2 * 365 + 1 and ctx["partial_last"] is True
    assert ctx["time"][0] * 1000 == ctx["window_start_ms"] == LISTING + (781 - 730) * DAY
    assert ctx["time"][-1] * 1000 == LISTING + 781 * DAY  # the forming replay day, nothing later
    assert ctx["close"][-1] == hourly_price(cursor) + 0.5 and ctx["price"] == ctx["close"][-1]
    assert ctx["high"][-1] == max(hourly_price(cursor - k * H) for k in range(7)) + 1
    lv = ctx["levels"]
    assert len(lv["resistance"]) <= 3 and len(lv["support"]) <= 3
    assert all(l["price"] > ctx["price"] for l in lv["resistance"]) and all(l["price"] <= ctx["price"] for l in lv["support"])
    assert [l["label"] for l in lv["resistance"]] == [f"R{i}" for i in range(1, len(lv["resistance"]) + 1)]
    # Indicators ride along and are settled from the warm-up days before the window.
    c2 = eng.btc_context(sid, years=1, indicators=[("ema", {"period": 20})])
    assert c2["count"] == 366 and c2["indicators"][0]["outputs"]["ema"][0] is not None
    with pytest.raises(Invalid):
        eng.btc_context(sid, years=6)
    with pytest.raises(Invalid):
        eng.btc_context(sid, tol_pct=11)
    # The BTC data was fetched on demand, and only up to the cursor's day.
    assert eng.candles.coverage("BTCUSDT", "1D") and all(c[1] <= LISTING + 780 * DAY for c in eng.candles.coverage("BTCUSDT", "1D"))


def test_fair_value_gaps_detection_and_fill():
    from app.replay.engine import fair_value_gaps
    #           c0      c1      c2 (bull gap 102-105 vs c0 high 102)   c3      c4 fills it     c5   c6 bear gap  c7
    rows = [
        (0, 100, 102, 99, 101), (1, 101, 104, 100, 103), (2, 104, 108, 105, 107), (3, 107, 109, 106, 108),
        (4, 108, 108, 101, 102),  # low 101 <= gap bottom 102: traded through -> filled here
        (5, 102, 103, 100, 101), (6, 101, 102, 96, 97), (7, 97, 98, 95, 96),  # c7 high 98 < c5 low 100: bear gap 98-100
    ]
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    gaps = fair_value_gaps(df, 0.0)
    assert [(g["dir"], g["bottom"], g["top"], g["filled"], g["t_start_ms"], g["t_end_ms"]) for g in gaps] == [
        ("bull", 102.0, 105.0, True, 1000, 4000),   # c0 high 102 vs c2 low 105
        ("bull", 104.0, 106.0, True, 2000, 4000),   # c1 high 104 vs c3 low 106
        ("bear", 103.0, 106.0, False, 4000, None),  # c3 low 106 vs c5 high 103
        ("bear", 98.0, 100.0, False, 6000, None),   # c5 low 100 vs c7 high 98
    ]
    assert gaps[0]["size_pct"] == pytest.approx(3 / 103 * 100)
    assert fair_value_gaps(df, 2.5) == [gaps[0], gaps[2]]  # the ~2 % gaps are filtered out
    assert fair_value_gaps(df.head(2), 0.0) == []
    with pytest.raises(Invalid):
        fair_value_gaps(df, 11.0)


def test_view_carries_fvg_only_when_asked(engine: ReplayEngine, session: dict):
    sid = session["id"]
    engine.step(sid, n=5)
    assert engine.view(sid, "1h", limit=20)["fvg"] is None
    v = engine.view(sid, "1h", limit=20, fvg_min_pct=0.0)
    assert isinstance(v["fvg"], list)
    for g in v["fvg"]:
        assert g["top"] > g["bottom"] and g["t_start_ms"] <= v["time"][-1] * 1000


PNG_1PX = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def test_trades_write_journal_rows_and_pictures(engine: ReplayEngine, session: dict):
    import base64
    sid = session["id"]
    engine.step(sid, n=20)
    b = engine.buy(sid, 400, interval="4h")
    snaps = engine.snapshots(sid)
    assert len(snaps) == 1 and snaps[0]["side"] == "buy" and snaps[0]["bucket_id"] == b["id"] and not snaps[0]["has_image"]
    r = snaps[0]["readings"]
    assert r["price"] == b["entry_price"] and r["rsi"] == b["buy_rsi"] and r["interval"] == "4h" and r["quote"] == 400
    assert r["cash_after"] == pytest.approx(10_000 - 400) and r["open_buckets_after"] == 1 and r["vs_last_buy_pct"] is None
    assert "bb_pctb" in r and "ema200_pct" in r and "realized_pnl" not in r
    # Pictures: only PNG data URLs of a sane size, for an existing row.
    for bad in ("data:image/png;base64,@@@", "data:image/png;base64," + base64.b64encode(b"notpng").decode(), "iVBORw0KGgo="):
        with pytest.raises(Invalid):
            engine.attach_snapshot_image(sid, b["id"], "buy", bad, None)
    with pytest.raises(Invalid):
        engine.attach_snapshot_image(sid, b["id"], "buy", "data:image/png;base64," + PNG_1PX, {"nested": {"a": 1}})
    with pytest.raises(NotFound):
        engine.attach_snapshot_image(sid, b["id"], "sell", "data:image/png;base64," + PNG_1PX, None)  # not sold yet
    snap = engine.attach_snapshot_image(sid, b["id"], "buy", "data:image/png;base64," + PNG_1PX, {"strategy": "rsi_reversal", "light_enter": True})
    assert snap["has_image"] and snap["readings"]["strategy"] == "rsi_reversal" and snap["readings"]["price"] == b["entry_price"]
    path = engine.snapshot_image_path(sid, snap["id"])
    assert path.is_file() and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # Selling writes the second row with the result and a copy of the buy readings.
    engine.step(sid, n=5)
    sold = engine.sell(sid, b["id"])
    snaps = engine.snapshots(sid)
    assert [x["side"] for x in snaps] == ["buy", "sell"]
    r2 = snaps[1]["readings"]
    assert r2["realized_pnl"] == pytest.approx(sold["realized_pnl"]) and r2["held_ms"] == sold["closed_ms"] - sold["opened_ms"]
    assert r2["realized_pnl_pct"] == pytest.approx(sold["realized_pnl_pct"]) and r2["price"] == sold["exit_price"]
    assert r2["at_buy"]["price"] == b["entry_price"] and r2["at_buy"]["strategy"] == "rsi_reversal"
    assert r2["vs_last_buy_pct"] is not None  # the bucket itself was the last buy
    text = engine.snapshots_csv(sid)
    lines = text.splitlines()
    assert len(lines) == 3 and lines[0].startswith("id,bucket_id,side,time_iso,interval")
    assert "realized_pnl" in lines[0] and "at_buy_price" in lines[0] and "at_buy_strategy" in lines[0]
    with pytest.raises(NotFound):
        engine.snapshot_image_path(sid, snaps[1]["id"])  # the sell has no picture yet
    engine.reset(sid)
    assert engine.snapshots(sid) == [] and not path.exists()


def test_daily_ema_overlay_uses_completed_days_only(tmp_path: Path):
    """With 300 days of history the overlay exists; intraday bars carry the previous day's value;
    the last value is the gauge's EMA 200 (same series)."""
    db = tmp_path / "ema.db"
    now = LISTING + 320 * DAY + 1
    eng = ReplayEngine(CandleStore(db, FakeClient(now)), SessionStore(db))
    s = eng.prepare(eng.create_session("BTCUSDT", LISTING + 300 * DAY, LISTING + 310 * DAY)["id"])
    sid = s["id"]
    eng.step(sid, n=30)  # 30 hours into the window
    assert eng.view(sid, "1h", limit=10)["daily_ema"] is None
    v = eng.view(sid, "1h", limit=40, daily_ema=True)
    ov = v["daily_ema"]
    assert ov["period"] == 200 and ov["interval"] == "1D" and len(ov["values"]) == v["count"]
    # The daily view shows the EMA of each day; the last one equals the gauge.
    dv = eng.view(sid, "1D", limit=5, daily_ema=True)
    daily_ema = dict(zip(dv["time"], dv["daily_ema"]["values"]))
    assert daily_ema[dv["time"][-1]] == pytest.approx(dv["gauges"]["ema_long"]["value"])
    for t, val in zip(v["time"], ov["values"]):
        prev_day = (t // 86400) * 86400 - 86400
        if prev_day in daily_ema and daily_ema[prev_day] is not None:
            assert val == pytest.approx(daily_ema[prev_day]), t
    assert any(x is not None for x in ov["values"])
    assert all(x is None or x > 0 for x in ov["values"])


def test_buy_whole_coins(engine: ReplayEngine, session: dict):
    sid = session["id"]
    engine.step(sid, n=3)
    price = engine.state(sid)["price"]
    fee_rate = 0.001
    # 250 USDT buys about 2 coins at a price near 100: rounded down to whole coins, the cost follows and
    # stays within the 250.
    b = engine.buy(sid, 250, whole=True)
    target = 250 * (1 - fee_rate) / price
    assert b["qty"] == float(math.floor(target)) and b["qty"] <= target and b["qty"] == int(b["qty"]) and b["cost"] <= 250
    assert b["cost"] == pytest.approx(b["qty"] * price / (1 - fee_rate)) and b["buy_fee"] == pytest.approx(b["cost"] * fee_rate)
    assert engine.state(sid)["cash"] == pytest.approx(10_000 - b["cost"])
    assert (b["cost"] - b["buy_fee"]) / price == pytest.approx(b["qty"])  # the usual math still holds
    # An explicit coin count: exact quantity, cost from the price, amount ignored.
    b3 = engine.buy(sid, 1, coins=4)
    assert b3["qty"] == 4.0 and b3["cost"] == pytest.approx(4 * price / (1 - fee_rate))
    with pytest.raises(Invalid, match="whole BTC cost"):
        engine.buy(sid, 1, coins=10_000)
    with pytest.raises(Invalid):
        engine.buy(sid, 1, coins=0)
    # All the cash: the order stays within it.
    cash = engine.state(sid)["cash"]
    b2 = engine.buy(sid, cash, whole=True)
    assert b2["qty"] == float(math.floor(cash * (1 - fee_rate) / price)) and b2["cost"] <= cash + 1e-9
    # Less than one whole coin affordable: refused, nothing changes.
    poor = engine.prepare(engine.create_session("BTCUSDT", START, END, start_balance=50)["id"])
    with pytest.raises(Invalid, match="one whole BTC"):
        engine.buy(poor["id"], 50, whole=True)
    assert engine.state(poor["id"])["cash"] == 50 and engine.state(poor["id"])["buckets"] == []
    assert ReplayEngine.whole_coin_order(250, 120.0, 0.001, 10_000) == (pytest.approx(2 * 120 / 0.999), 2)
    assert ReplayEngine.whole_coin_order(1500, 1.03, 0.001, 10_000) == (pytest.approx(1454 * 1.03 / 0.999), 1454)  # 1454.85 coins -> 1454
    assert ReplayEngine.whole_coin_order(250, 120.0, 0.001, 130) == (pytest.approx(1 * 120 / 0.999), 1)  # capped by cash
    assert ReplayEngine.whole_coin_order(50, 120.0, 0.001, 50)[1] == 0


def test_reference_is_empty_before_any_trade(engine: ReplayEngine, session: dict):
    ref = engine.state(session["id"])["reference"]
    assert ref == {"last_buy": None, "last_sell": None, "last_closed": None}


def _expected_reference(trade_price: float, trade_ms: int, cursor_ms: int) -> tuple[str, float]:
    """(basis, pct) the way a human would compute it from the synthetic closes."""
    closes = [(close_at(t), t) for t in range(trade_ms + H, cursor_ms + 1, H)]
    peak = max(closes)[0] if closes else None
    basis = peak if peak is not None and peak > trade_price else trade_price
    return ("peak" if basis != trade_price else "price"), (close_at(cursor_ms) / basis - 1) * 100


def test_reference_tracks_the_peak_since_the_trade(engine: ReplayEngine, session: dict):
    """Synthetic closes rise 1/h for 50h then drop: the basis switches to the running high and back."""
    sid = session["id"]
    b = engine.buy(sid, 500)
    for n in (1, 4, 30, 60):
        engine.step(sid, n=n)
        st = engine.state(sid)
        basis, pct = _expected_reference(b["entry_price"], START, st["cursor_ms"])
        ref = st["reference"]["last_buy"]
        assert ref["basis"] == basis and ref["pct"] == pytest.approx(pct), (n, ref)
        if basis == "peak":
            assert ref["peak"] > b["entry_price"] and ref["peak"] == close_at(ref["peak_ms"])
            assert ref["pct"] <= 0.0  # never above the height
        else:
            assert ref["peak"] is None and ref["peak_ms"] is None
    # Sell at the top of the saw-tooth: nothing open, basis follows the high after the sell.
    sold = engine.sell(sid, b["id"])
    sold_ms = engine.state(sid)["cursor_ms"]
    engine.step(sid, n=20)
    st = engine.state(sid)
    basis, pct = _expected_reference(sold["exit_price"], sold_ms, st["cursor_ms"])
    ref = st["reference"]["last_sell"]
    assert ref["basis"] == basis and ref["pct"] == pytest.approx(pct)
    assert st["reference"]["last_buy"]["seq"] == 1  # the sold bucket still counts as the last buy



def test_reset_restores_everything(engine: ReplayEngine, session: dict):
    sid = session["id"]
    engine.buy(sid, 500)
    engine.step(sid, n=20)
    st = engine.reset(sid)
    assert st["cursor_ms"] == START and st["cash"] == 10_000 and st["buckets"] == []
    assert engine.trades(sid) == {"trades": [], "buckets": []}
    assert engine.buy(sid, 100)["seq"] == 1  # numbering restarts


# ------------------------------------------------------------------------ view
def test_view_never_shows_the_future(engine: ReplayEngine, session: dict):
    sid = session["id"]
    engine.step(sid, n=5)
    cursor = START + 5 * H
    v = engine.view(sid, "1h", limit=50)
    assert v["count"] == 50 and v["time"][-1] == cursor // 1000 and v["partial_last"] is False
    assert v["close"][-1] == close_at(cursor)
    v4 = engine.view(sid, "4h", limit=10)
    assert all(t * 1000 + 4 * H <= cursor + H or t * 1000 == (cursor // (4 * H)) * 4 * H for t in v4["time"])
    assert v4["time"][-1] * 1000 <= cursor


def test_view_partial_candle_is_aggregated_from_base(engine: ReplayEngine, session: dict):
    sid = session["id"]
    engine.step(sid, n=2)  # cursor = START + 2h: the 4h candle [START, START+4h) has 3 of 4 hours
    v = engine.view(sid, "4h", limit=3)
    assert v["partial_last"] is True
    assert v["time"][-1] == START // 1000
    hours = [START, START + H, START + 2 * H]
    assert v["open"][-1] == hourly_price(hours[0])
    assert v["close"][-1] == close_at(hours[-1])
    assert v["high"][-1] == max(hourly_price(h) for h in hours) + 1
    assert v["low"][-1] == min(hourly_price(h) for h in hours) - 1
    assert v["volume"][-1] == 30.0
    # Previous 4h candle is the exchange's own closed candle.
    assert v["time"][-2] == (START - 4 * H) // 1000
    engine.step(sid)  # cursor = START + 3h: the 4h candle is now complete
    v = engine.view(sid, "4h", limit=1)
    assert v["partial_last"] is False and v["time"][-1] == START // 1000
    assert v["close"][-1] == close_at(START + 3 * H) and v["volume"][-1] == 40.0


def test_aggregate_taker_buy_is_unknown_when_one_base_candle_lacks_it():
    from app.replay.engine import aggregate
    base = pd.DataFrame({"open": [1.0, 2.0], "high": [2.0, 3.0], "low": [0.5, 1.5], "close": [2.0, 2.5],
                         "volume": [4.0, 6.0], "taker_buy": [3.0, 1.0]})
    assert aggregate(base, 0)["taker_buy"] == 4.0
    base.loc[1, "taker_buy"] = float("nan")
    assert math.isnan(aggregate(base, 0)["taker_buy"])


def test_view_volume_delta_fills_old_candles_ahead_of_the_cursor(engine: ReplayEngine, session: dict):
    """Candles stored before taker-buy volumes were kept: the view fills them in the background,
    past the cursor too, so after a step the newest bar has its delta at once (filling only up to
    the cursor left the newest bar empty after every step)."""
    import time
    sid, store = session["id"], engine.candles
    with store._conn() as conn:
        conn.execute("UPDATE candles SET taker_buy=NULL")
    v = engine.view(sid, "1h", limit=5, indicators=[("vdelta", {})])
    assert v["indicators"][0]["outputs"]["delta"][-1] is None   # not filled yet: the view never waits
    for _ in range(500):
        if not store._fill_running:
            break
        time.sleep(0.01)
    cursor = session["cursor_ms"]
    assert store.missing_taker_buy("BTCUSDT", "1h", cursor - 4 * H, END) is None   # the stored data ahead is filled
    engine.step(sid)
    v = engine.view(sid, "1h", limit=5, indicators=[("vdelta", {})])
    assert v["indicators"][0]["outputs"]["delta"] == pytest.approx([2.0] * 5)      # the newest bar included


def test_view_volume_delta_also_on_the_forming_candle(engine: ReplayEngine, session: dict):
    """The vdelta indicator: 2 x taker buy - volume per bar, the forming 4h candle summed from its
    base candles (the fake's taker buys are 60 % of the volume: delta = 20 % of it)."""
    sid = session["id"]
    v = engine.view(sid, "4h", limit=3, indicators=[("vdelta", {})])
    assert v["partial_last"] is True and v["volume"][-1] == 10.0
    assert v["indicators"][0]["key"] == "vdelta"
    assert v["indicators"][0]["outputs"]["delta"] == pytest.approx([8.0, 8.0, 2.0])


def test_view_indicators_are_stable_as_cursor_advances(engine: ReplayEngine, session: dict):
    sid = session["id"]
    engine.step(sid, n=30)
    a = engine.view(sid, "1h", limit=20, indicators=[("ema", {"period": 20})], strategy=("ema_cross", {"fast": 5, "slow": 20}))
    engine.step(sid, n=40)
    b = engine.view(sid, "1h", limit=60, indicators=[("ema", {"period": 20})], strategy=("ema_cross", {"fast": 5, "slow": 20}))
    ia = dict(zip(a["time"], a["indicators"][0]["outputs"]["ema"]))
    ib = dict(zip(b["time"], b["indicators"][0]["outputs"]["ema"]))
    sa = dict(zip(a["time"], a["strategy"]["state"]))
    sb = dict(zip(b["time"], b["strategy"]["state"]))
    for t in ia:
        assert ib[t] == pytest.approx(ia[t]) and sb[t] == sa[t]
    # And the EMA is the real EMA over the full warm-up history, not a windowed one.
    full = engine.candles.get_candles("BTCUSDT", "1h", end_ms=START + 70 * H)
    assert ib[b["time"][-1]] == pytest.approx(core.ema(full["close"], 20).iloc[-1])


def test_view_rsi_panes_follow_the_bars(engine: ReplayEngine, session: dict):
    """Extra RSI panes: one value per bar on screen, never from a candle that closes after the bar."""
    sid = session["id"]
    engine.step(sid, n=30)   # cursor = START + 30h: the 4h candle [START+28h, START+32h) is forming
    v = engine.view(sid, "1h", limit=24, indicators=[("rsi", {"period": 14})], rsi_panes=["1h", "4h"])
    assert [p["interval"] for p in v["rsi_panes"]] == ["1h", "4h"] and all(p["period"] == 14 for p in v["rsi_panes"])
    own, slow = v["rsi_panes"]
    assert len(own["values"]) == len(slow["values"]) == v["count"] == 24
    # the pane of the chart's own timeframe is the RSI indicator itself
    for a, b in zip(own["values"], v["indicators"][0]["outputs"]["rsi"]):
        assert a == pytest.approx(b, rel=1e-9)
    # the 4h pane on the 1h chart: a step line that only changes where a 4h candle closes,
    # and the latest bar shows the forming 4h candle's RSI (what the 4h chart shows now)
    four = engine.view(sid, "4h", limit=3, indicators=[("rsi", {"period": 14})])
    assert four["partial_last"] is True
    assert slow["values"][-1] == pytest.approx(four["indicators"][0]["outputs"]["rsi"][-1], rel=1e-9)
    times = v["time"]
    for i in range(1, 23):
        bar_close = times[i] * 1000 + H
        if bar_close % (4 * H) != 0:   # no 4h candle closes with this bar: same value as the bar before
            assert slow["values"][i] == slow["values"][i - 1]
        else:
            assert slow["values"][i] != slow["values"][i - 1]
    # the closed 4h value shown at a 4h close is the RSI over the 4h candles up to that close
    i = next(k for k in range(23) if (times[k] * 1000 + H) % (4 * H) == 0)
    hist = engine.candles.get_candles("BTCUSDT", "4h", end_ms=times[i] * 1000 + H - 4 * H)
    assert slow["values"][i] == pytest.approx(core.rsi(hist["close"], 14).iloc[-1], rel=1e-6)
    # a finer pane on a coarser chart: each 4h bar shows the 1h RSI at that bar's close
    v4 = engine.view(sid, "4h", limit=4, rsi_panes=["1h"])
    fine = v4["rsi_panes"][0]["values"]
    for k in range(3):   # the closed 4h bars
        upto = engine.candles.get_candles("BTCUSDT", "1h", end_ms=v4["time"][k] * 1000 + 4 * H - H)
        assert fine[k] == pytest.approx(core.rsi(upto["close"], 14).iloc[-1], rel=1e-6)
    assert fine[-1] == pytest.approx(own["values"][-1], rel=1e-9)   # the forming 4h bar: the 1h RSI now
    assert engine.view(sid, "1h", limit=5)["rsi_panes"] == []
    assert engine.view(sid, "1h", limit=5, rsi_panes=["4h", "4h"])["rsi_panes"][0]["interval"] == "4h"
    with pytest.raises(Invalid):
        engine.view(sid, "1h", rsi_panes=["5m"])   # finer than the base
    with pytest.raises(Invalid):
        engine.view(sid, "1h", rsi_panes=["7m"])   # unknown
    with pytest.raises(Invalid):
        engine.view(sid, "1h", rsi_panes=["1h", "2h", "4h", "6h", "8h"])


def test_view_validation(engine: ReplayEngine, session: dict):
    sid = session["id"]
    with pytest.raises(Invalid):
        engine.view(sid, "5m")
    with pytest.raises(Invalid):
        engine.view(sid, indicators=[("nope", {})])
    with pytest.raises(Invalid):
        engine.view(sid, strategy=("ema_cross", {"fast": 50, "slow": 20}))
    with pytest.raises(Invalid):
        engine.view(sid, limit=0)


def _day(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def test_extremes_windows():
    """Highest high and lowest low (wicks) per window ending with the replay day, with distances and dates."""
    end_open = LISTING + 999 * DAY
    high = [100.0] * 1000
    low = [90.0] * 1000
    high[600], high[800], high[950] = 200.0, 150.0, 120.0   # all-time / 1-year / 3-month high
    low[100], low[700], low[980] = 50.0, 70.0, 85.0          # all-time / 1-year / 3-month low
    df = pd.DataFrame({"time": [(LISTING + i * DAY) // 1000 for i in range(1000)], "open": 95.0, "high": high, "low": low, "close": 95.0, "volume": 1.0})
    ex = ReplayEngine.extremes(df, 95.0, end_open)
    a, y, h, m = ex["all"], ex["1y"], ex["6m"], ex["3m"]
    assert (a["days"], a["high"], a["low"], a["bars"], a["from_ms"]) == (None, 200.0, 50.0, 1000, LISTING)
    assert a["high_ms"] == LISTING + 600 * DAY and a["high_iso"][:10] == _day(LISTING + 600 * DAY)
    assert a["low_ms"] == LISTING + 100 * DAY and a["low_iso"][:10] == _day(LISTING + 100 * DAY)
    assert a["vs_high_pct"] == pytest.approx((95 / 200 - 1) * 100) and a["vs_low_pct"] == pytest.approx((95 / 50 - 1) * 100)
    assert a["position"] == pytest.approx(45 / 150)
    # A year is the replay day plus the 365 days before it; three months the replay day plus 90.
    assert (y["days"], y["high"], y["low"], y["bars"]) == (365, 150.0, 70.0, 366) and y["from_ms"] == end_open - 365 * DAY
    assert (m["days"], m["high"], m["low"], m["bars"]) == (90, 120.0, 85.0, 91) and m["from_ms"] == end_open - 90 * DAY
    # Half a year is the replay day plus the 182 days before it: the 1-year high (199 days back) is outside it.
    assert (h["days"], h["high"], h["low"], h["bars"]) == (182, 120.0, 85.0, 183) and h["from_ms"] == end_open - 182 * DAY
    assert m["vs_high_pct"] == pytest.approx((95 / 120 - 1) * 100) and m["position"] == pytest.approx(10 / 35)
    # Ties go to the earliest day; at the high the position is 1 and the distance 0.
    flat = ReplayEngine.extremes(df.iloc[-10:], 100.0, end_open)
    assert flat["3m"]["high"] == 100.0 and flat["3m"]["high_ms"] == LISTING + 990 * DAY and flat["3m"]["bars"] == 10
    assert flat["3m"]["vs_high_pct"] == 0.0 and flat["3m"]["position"] == 1.0
    # No candles: empty readings, no crash.
    empty = ReplayEngine.extremes(df.iloc[0:0], 95.0, end_open)
    assert empty["all"]["high"] is None and empty["1y"]["vs_low_pct"] is None and empty["3m"]["bars"] == 0


def test_highs_and_lows_reach_back_to_the_listing(db: Path):
    """The card uses every daily candle since the Binance listing, not just the session's warm-up,
    downloads them once, and the journal keeps the six distances with prices and dates."""
    now = LISTING + 1500 * DAY + 1
    client = FakeClient(now)
    eng = ReplayEngine(CandleStore(db, client), SessionStore(db))
    start = LISTING + 1400 * DAY
    s = eng.create_session("ETHUSDT", start, start + 5 * DAY, base_interval="1h", start_balance=10_000, fee_rate=0.001)
    sid = s["id"]
    eng.prepare(sid)
    stats = {(r["symbol"], r["interval"]): r for r in eng.candles.stats()}
    assert stats[("ETHUSDT", "1D")]["first_ms"] == start - 1000 * DAY  # preparing fetched the warm-up only
    eng.step(sid, n=3)
    g = eng.view(sid, "1h", limit=3)["gauges"]
    ex = g["extremes"]
    assert ex["complete"] and ex["interval"] == "1D"
    a = ex["all"]
    assert a["from_ms"] == LISTING and a["bars"] == 1401  # 1400 closed days and the forming one
    assert eng.candles.stats() and {(r["symbol"], r["interval"]): r for r in eng.candles.stats()}[("ETHUSDT", "1D")]["first_ms"] == LISTING
    price = g["price"]
    assert (a["high"], a["low"]) == (150.0, 99.0)  # the fake exchange's range: hourly 100..149, wicks +-1
    assert a["vs_high_pct"] == pytest.approx((price / 150 - 1) * 100) and a["vs_low_pct"] == pytest.approx((price / 99 - 1) * 100)
    assert 0 <= a["position"] <= 1 and a["high_ms"] >= LISTING and a["high_iso"][:10] == _day(a["high_ms"])
    assert ex["1y"]["bars"] == 366 and ex["3m"]["bars"] == 91
    assert ex["1y"]["from_ms"] == start - 365 * DAY and ex["3m"]["from_ms"] == start - 90 * DAY
    # Downloaded once: stepping into the next days asks the exchange for nothing more.
    calls = len(client.calls)
    eng.step(sid, n=30)
    eng.view(sid, "1h", limit=3)
    assert len(client.calls) == calls
    # Journal rows carry price, distance and date of each high and low.
    b = eng.buy(sid, 500)
    r = eng.snapshots(sid)[0]["readings"]
    now_ex = eng.view(sid, "1h", limit=1)["gauges"]["extremes"]
    for key, (hk, lk) in (("all", ("ath", "atl")), ("1y", ("high_1y", "low_1y")), ("3m", ("high_3m", "low_3m"))):
        w = now_ex[key]
        assert r[hk] == w["high"] and r[hk + "_pct"] == pytest.approx(w["vs_high_pct"]) and r[hk + "_date"] == w["high_iso"][:10]
        assert r[lk] == w["low"] and r[lk + "_pct"] == pytest.approx(w["vs_low_pct"]) and r[lk + "_date"] == w["low_iso"][:10]
    assert r["ath_pct"] <= 0 <= r["atl_pct"]
    eng.sell(sid, b["id"])
    header = eng.snapshots_csv(sid).splitlines()[0]
    assert "ath_pct" in header and "low_3m_date" in header and "at_buy_ath_pct" in header


def test_highs_and_lows_survive_a_failed_history_download(db: Path):
    """When the days before the warm-up cannot be fetched, the stored days are used, the reading is
    marked incomplete and the exchange is not hammered on every step."""
    now = LISTING + 1500 * DAY + 1
    client = FakeClient(now)
    eng = ReplayEngine(CandleStore(db, client), SessionStore(db))
    start = LISTING + 1400 * DAY
    sid = eng.create_session("ETHUSDT", start, start + 5 * DAY, base_interval="1h", start_balance=10_000, fee_rate=0.001)["id"]
    eng.prepare(sid)

    def boom(*a, **k):
        raise OSError("offline")

    client.klines = boom
    ex = eng.view(sid, "1h", limit=3)["gauges"]["extremes"]
    assert ex["complete"] is False and ex["all"]["from_ms"] == start - 1000 * DAY and ex["all"]["bars"] == 1001
    eng.step(sid, n=2)
    assert eng.view(sid, "1h", limit=3)["gauges"]["extremes"]["complete"] is False  # no retry within the minute
    eng._history_retry.clear()
    client.klines = FakeClient(now).klines
    ex = eng.view(sid, "1h", limit=3)["gauges"]["extremes"]
    assert ex["complete"] and ex["all"]["from_ms"] == LISTING


def test_long_average_changes_between_buy_and_sell(db: Path):
    """A closed bucket says how the daily EMA/SMA 200 moved and how the price's distance to them changed."""
    now = LISTING + 1500 * DAY + 1
    engine = ReplayEngine(CandleStore(db, FakeClient(now)), SessionStore(db))
    start = LISTING + 1400 * DAY
    sid = engine.create_session("BTCUSDT", start, start + 5 * DAY, base_interval="1h", start_balance=10_000, fee_rate=0.001)["id"]
    engine.prepare(sid)
    engine.step(sid, n=20)
    b = engine.buy(sid, 400)
    g = engine.view(sid, "1h", limit=1)["gauges"]
    assert b["buy_ema200"] == g["ema_long"]["value"] and b["buy_ema200_pct"] == g["ema_long"]["price_pct"]
    assert b["buy_sma200"] is None   # the SMA 200 is no longer recorded (old column, kept for old rows)
    assert b["sell_ema200"] is None and b["ema200_change_pct"] is None and b["sma200_dist_change"] is None
    engine.step(sid, n=48)
    g2 = engine.view(sid, "1h", limit=1)["gauges"]
    sold = engine.sell(sid, b["id"])
    assert sold["sell_ema200"] == g2["ema_long"]["value"] and sold["sell_sma200_pct"] is None
    for name, gauge in (("ema200", "ema_long"),):
        before, after = g[gauge]["value"], g2[gauge]["value"]
        assert before is not None and after is not None  # 1400 days of history: the 200-day lines exist
        assert sold[f"{name}_change_pct"] == pytest.approx((after / before - 1) * 100)
        assert sold[f"{name}_dist_change"] == pytest.approx(g2[gauge]["price_pct"] - g[gauge]["price_pct"])
    # The sell's journal row carries the same four numbers; the CSV too.
    r = engine.snapshots(sid)[1]["readings"]
    for k in ("ema200_change_pct", "ema200_dist_change"):
        assert r[k] == sold[k] or r[k] == pytest.approx(sold[k])
    assert "ema200_dist_change" in engine.snapshots_csv(sid).splitlines()[0]
    # The pure rule, with numbers.
    ch = ReplayEngine.long_avg_changes({"ema200": 100.0, "ema200_pct": -5.0}, {"ema200": 110.0, "ema200_pct": 3.0})
    assert ch == {"ema200_change_pct": pytest.approx(10.0), "ema200_dist_change": pytest.approx(8.0)}
    assert ReplayEngine.long_avg_changes({"ema200": None}, {"ema200": 1.0})["ema200_change_pct"] is None


def test_old_database_gets_long_average_columns_from_the_journal(tmp_path: Path):
    """Buckets closed before the EMA/SMA columns existed get their values back from the journal rows."""
    import json
    import sqlite3
    from app.replay.session_store import LONG_AVG_COLUMNS, SCHEMA, SessionStore
    old = tmp_path / "old.db"
    legacy = SCHEMA
    for col in LONG_AVG_COLUMNS:
        legacy = "\n".join(line for line in legacy.splitlines() if not line.strip().startswith(col))
    legacy = legacy.replace("sell_rsi_interval TEXT,", "sell_rsi_interval TEXT")
    conn = sqlite3.connect(old)
    conn.executescript(legacy)
    conn.execute("INSERT INTO sessions(id, name, symbol, base_interval, start_ms, end_ms, cursor_ms, start_balance, cash, fee_rate, status, created_at, updated_at) "
                 "VALUES ('s1', 'n', 'BTCUSDT', '1h', 0, 1, 0, 1000, 1000, 0.001, 'ready', 'x', 'x')")
    conn.execute("INSERT INTO buckets(id, session_id, seq, status, opened_ms, entry_price, qty, cost, buy_fee, closed_ms, exit_price, proceeds, sell_fee, realized_pnl) "
                 "VALUES (7, 's1', 1, 'closed', 0, 10, 1, 10, 0.01, 1, 11, 11, 0.01, 0.98)")
    for side, t, ema, pct in (("buy", 0, 9.0, 11.1), ("sell", 1, 9.9, 11.1)):
        conn.execute("INSERT INTO snapshots(session_id, bucket_id, side, time_ms, interval, image_path, readings, created_at) VALUES ('s1', 7, ?, ?, '1h', NULL, ?, 'x')",
                     (side, t, json.dumps({"ema200": ema, "ema200_pct": pct, "sma200": None, "sma200_pct": None})))
    conn.commit()
    conn.close()
    SessionStore(old)
    c = sqlite3.connect(old)
    c.row_factory = sqlite3.Row
    row = dict(c.execute("SELECT * FROM buckets WHERE id=7").fetchone())
    c.close()
    assert (row["buy_ema200"], row["sell_ema200"], row["buy_ema200_pct"], row["buy_sma200"]) == (9.0, 9.9, 11.1, None)
    SessionStore(old)  # idempotent


def test_nearest_levels_are_zones_with_counted_visits():
    """Next resistance/support as tubes of +-tolerance around the swing levels: visits are counted as
    touches (left on the side they came from within 5 days), breaks (left on the other side) or
    stays (longer inside); the distance is measured to the nearer edge."""
    from app.replay.engine import nearest_levels, zone_visits
    day = 86400
    rows = []
    for i in range(120):
        hi, lo, cl = 100.0, 95.0, 98.0
        if i in (20, 50, 80): hi = 110.0 + (i == 50) * 1.0       # three swing highs near 110 (within 1.5 %)
        if i in (35, 65): hi = 130.0                               # two swings at 130: on the way they cross the 110 tube
        if i in (25, 55, 95): lo = 90.0                            # three swing lows at 90
        if i == 70: lo = 80.0                                      # one swing at 80: its candle also dips into the 90 tube
        if i == 100: hi, cl = 115.0, 114.0                         # closes above the 110 tube ...
        if i == 101: hi, lo, cl = 118.0, 113.0, 115.0              # ... and the next day stays above: a break
        if 105 <= i <= 111: hi, lo, cl = 110.0, 105.0, 108.0       # seven days inside the tube: a stay
        rows.append((i * day, 98.0, hi, lo, cl))
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
    lv = nearest_levels(df, 100.0)
    assert lv["min_touches"] == 3 and lv["tol_pct"] == 1.5 and lv["max_visit_bars"] == 5 and lv["levels"] == 3
    r, s = lv["resistance"], lv["support"]
    centre = (110 + 111 + 110) / 3
    assert r["price"] == pytest.approx(centre) and r["low"] == pytest.approx(centre * 0.985) and r["high"] == pytest.approx(centre * 1.015)
    assert (r["touches"], r["breaks"], r["stays"], r["inside"]) == (5, 1, 1, False)
    assert r["pct"] == pytest.approx((r["low"] / 100 - 1) * 100)  # to the lower edge, not the centre
    assert r["touch_dates"] == [_day(d * day * 1000) for d in (20, 35, 50, 65, 80)]
    assert r["first_iso"][:10] == r["touch_dates"][0] and r["last_iso"][:10] == r["touch_dates"][-1] and r["last_ms"] == 80 * day * 1000
    assert [(v["date"][8:], v["from"], v["bars"], v["result"]) for v in r["visits"]][-2:] == [("11", "below", 1, "break"), ("16", "below", 7, "stay")]
    assert s["price"] == 90.0 and (s["touches"], s["breaks"], s["inside"]) == (4, 0, False)
    assert s["pct"] == pytest.approx((s["high"] / 100 - 1) * 100) and s["touch_dates"] == [_day(d * day * 1000) for d in (25, 55, 70, 95)]
    # Inside a tube: distance 0 and the flag; below everything: no support, the 90 tube is the resistance.
    inside = nearest_levels(df, 110.0)["resistance"]
    assert inside["price"] == pytest.approx(centre) and inside["inside"] is True and inside["pct"] == 0.0
    assert nearest_levels(df, 85.0)["support"] is None and nearest_levels(df, 85.0)["resistance"]["price"] == 90.0
    assert nearest_levels(df.head(5), 100.0) == {"resistance": None, "support": None, "levels": 0, "swings": 0,
                                                  "min_touches": 3, "tol_pct": 1.5, "max_visit_bars": 5}
    with pytest.raises(Invalid):
        nearest_levels(df, 100.0, tol_pct=0)
    # zone_visits alone: a visit open at the end and one starting on the first candle are not counted.
    v = zone_visits(df, 88.65, 91.35)
    assert [x["result"] for x in v] == ["touch"] * 4 and all(x["from"] == "above" for x in v)
    tail = df.iloc[95:98].reset_index(drop=True)   # starts on the candle in the tube (day 95)
    assert zone_visits(tail, 88.65, 91.35) == []
    assert zone_visits(df.iloc[90:96], 88.65, 91.35) == []  # the visit on day 95 is still open


def test_level_readings_in_view_and_journal(db: Path):
    """The view carries the next levels of the last year's closed days with the requested tolerance; trades record them."""
    now = LISTING + 1500 * DAY + 1
    engine = ReplayEngine(CandleStore(db, FakeClient(now)), SessionStore(db))
    start = LISTING + 1400 * DAY
    sid = engine.create_session("BTCUSDT", start, start + 5 * DAY, base_interval="1h", start_balance=10_000, fee_rate=0.001)["id"]
    engine.prepare(sid)
    engine.step(sid, n=5)
    lv = engine.view(sid, "1h", limit=3)["gauges"]["levels"]
    assert lv["interval"] == "1D" and lv["days"] == 365 and lv["bars"] == 365 and lv["tol_pct"] == 1.5 and lv["min_touches"] == 3 and lv["max_visit_bars"] == 5
    assert lv["from_iso"][:10] == _day(start - 365 * DAY)  # the fake exchange's daily highs plateau: it may find no swing
    price = engine.state(sid)["price"]
    for key in ("resistance", "support"):
        l = lv[key]
        if l is not None:
            assert l["touches"] >= 3 and len(l["touch_dates"]) == l["touches"] and (l["price"] > price) == (key == "resistance")
            assert l["low"] < l["price"] < l["high"] and len(l["visits"]) >= l["touches"] + l["breaks"] + l["stays"]
            edge = l["low"] if key == "resistance" else l["high"]
            assert l["pct"] == (0.0 if l["inside"] else pytest.approx((edge / price - 1) * 100))
    assert engine.view(sid, "1h", limit=3, sr_tol=4.0)["gauges"]["levels"]["tol_pct"] == 4.0
    b = engine.buy(sid, 500, sr_tol=4.0)
    r = engine.snapshots(sid)[0]["readings"]
    lv4 = engine.view(sid, "1h", limit=1, sr_tol=4.0)["gauges"]["levels"]
    for key, name in (("resistance", "next_resistance"), ("support", "next_support")):
        l = lv4[key]
        assert r[name] == (l["price"] if l else None) and r[f"{name}_touches"] == (l["touches"] if l else None)
        assert r[f"{name}_low"] == (l["low"] if l else None) and r[f"{name}_high"] == (l["high"] if l else None)
        assert r[f"{name}_breaks"] == (l["breaks"] if l else None)
        assert r[f"{name}_last"] == (l["last_iso"][:10] if l else None) and r[f"{name}_pct"] == (pytest.approx(l["pct"]) if l else None)
    engine.sell(sid, b["id"])
    assert "next_support_touches" in engine.snapshots_csv(sid).splitlines()[0]


def test_high_and_low_while_held(engine: ReplayEngine, session: dict):
    """Open buckets carry the running high/low since the buy; a sell freezes them (buy candle to sell
    candle, wicks); the journal sell row and the CSV have them; old closed buckets get them on listing."""
    sid = session["id"]
    engine.step(sid, n=10)
    b = engine.buy(sid, 400)
    t0 = b["opened_ms"]
    assert b["held_high"] == hourly_price(t0) + 1 and b["held_low"] == hourly_price(t0) - 1 and b["held_high_ms"] == t0
    assert b["held_high_pct"] == pytest.approx((b["held_high"] / b["entry_price"] - 1) * 100)
    engine.step(sid, n=30)
    cursor = engine.state(sid)["cursor_ms"]
    hours = range(t0, cursor + 1, H)
    hi, lo = max(hourly_price(t) + 1 for t in hours), min(hourly_price(t) - 1 for t in hours)
    ob = engine.state(sid)["buckets"][0]
    assert ob["held_high"] == hi and ob["held_low"] == lo and ob["held_high_iso"] is not None
    assert ob["held_high_ms"] == min(t for t in hours if hourly_price(t) + 1 == hi) and ob["held_low_ms"] == min(t for t in hours if hourly_price(t) - 1 == lo)
    listed = [x for x in engine.trades(sid)["buckets"] if x["id"] == b["id"]][0]
    assert listed["held_high"] == hi and listed["held_low_pct"] == pytest.approx((lo / b["entry_price"] - 1) * 100)
    sold = engine.sell(sid, b["id"])
    assert sold["held_high"] == hi and sold["held_low"] == lo and sold["held_low_pct"] == pytest.approx((lo / b["entry_price"] - 1) * 100)
    engine.step(sid, n=5)  # later candles do not change a closed bucket
    closed = [x for x in engine.trades(sid)["buckets"] if x["id"] == b["id"]][0]
    assert closed["held_high"] == hi and closed["held_low"] == lo
    r = engine.snapshots(sid)[1]["readings"]
    assert r["held_high"] == hi and r["held_low"] == lo and r["held_high_pct"] == pytest.approx(sold["held_high_pct"])
    assert r["held_high_time"] == closed["held_high_iso"] and "held_low_pct" in engine.snapshots_csv(sid).splitlines()[0]
    # A bucket closed before the columns existed: filled from the candles on the next listing.
    with engine.sessions.tx() as conn:
        engine.sessions.update_bucket(conn, b["id"], held_high=None, held_high_ms=None, held_low=None, held_low_ms=None)
    again = [x for x in engine.trades(sid)["buckets"] if x["id"] == b["id"]][0]
    assert again["held_high"] == hi and again["held_low_ms"] == closed["held_low_ms"]
