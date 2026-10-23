"""Day-trading mode: the spec's section 14 numbers, the Malta clock, and the engine against a
15-minute fake exchange (no network)."""
from __future__ import annotations

import base64
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.data.candle_store import CandleStore
from app.indicators import core
from app.replay.day_engine import (
    CONDITIONS,
    DAY_NOTES_MAX_CHARS,
    STOP_ATR_MULT,
    DayEngine,
    alt_window_end,
    day_bounds,
    in_entry_window,
    local_ms,
    picture_frame,
    position_size,
    reward_to_risk,
    to_local,
    trade_result,
    trading_day,
    what_if,
)
from app.replay.day_store import DayStore
from app.replay.engine import Conflict, Invalid, NotFound, ReplayEngine
from app.replay.session_store import SessionStore
from app.indicators.registry import validate_params
from app.strategies.rline import RLINE_PARAMS, get_jump_strategy
from tests.fakes import DAY, H, LISTING, Q, FineFakeClient, quarter_candle

START = LISTING + 300 * DAY          # 2018-05-10 00:00 UTC (Malta summer time: 06:00 local = 04:00 UTC)
END = START + 6 * DAY
NOW = START + 10 * DAY + 1
FIRST_CURSOR = START + 3 * H + 45 * 60_000   # the 15m candle closing at 06:00 local on 2018-05-10
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 16).decode()


def utc_ms(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


def make(db: Path, overrides: dict | None = None) -> DayEngine:
    replay = ReplayEngine(CandleStore(db, FineFakeClient(NOW, overrides)), SessionStore(db))
    return DayEngine(replay, DayStore(db))


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "replay.db"


@pytest.fixture
def day(db: Path) -> DayEngine:
    return make(db)


@pytest.fixture
def session(day: DayEngine) -> dict:
    s = day.create_session("BTCUSDT", START, END, start_balance=100_000)
    assert s["status"] == "preparing" and s["mode"] == "day"
    return day.prepare(s["id"])


def local_hm(ms: int) -> tuple[date, int, int]:
    loc = to_local(ms)
    return loc.date(), loc.hour, loc.minute


# ------------------------------------------------------------ section 14 numbers
def test_spec_stop_and_position_size():
    assert 98.60 - STOP_ATR_MULT * 1.20 == pytest.approx(98.00)
    size = position_size(100.0, 100.0, 98.0, 0.001, 10_000.0)
    assert size["coins"] == pytest.approx(50.0)
    assert size["position_value"] == pytest.approx(5_000.0)
    assert size["buy_fee"] == pytest.approx(5.00)
    assert size["risk_usdt"] == pytest.approx(100.0)
    assert size["capped"] is False
    assert reward_to_risk(100.0, 98.0, 104.0) == pytest.approx(2.0)


def test_spec_exit_at_stop():
    r = trade_result(100.0, 98.0, 50.0, 5_000.0, 5.0, 0.001, 100.0)
    assert r["sell_value"] == pytest.approx(4_900.0) and r["sell_fee"] == pytest.approx(4.90)
    assert r["pnl"] == pytest.approx(-109.90)
    assert r["r_multiple"] == pytest.approx(-1.099)
    assert round(r["pnl_pct"], 2) == -2.20


def test_spec_exit_at_take_profit():
    r = trade_result(100.0, 104.0, 50.0, 5_000.0, 5.0, 0.001, 100.0)
    assert r["sell_value"] == pytest.approx(5_200.0) and r["sell_fee"] == pytest.approx(5.20)
    assert r["pnl"] == pytest.approx(189.80)
    assert r["r_multiple"] == pytest.approx(1.898)
    assert round(r["pnl_pct"], 2) == 3.80


def test_spec_what_if():
    assert what_if(100.0, 101.0, 103.50, 99.20) == {
        "whatif_above_exit_pct": 2.48, "whatif_below_exit_pct": -1.78,
        "whatif_high_vs_entry_pct": 3.50, "whatif_low_vs_entry_pct": -0.80,
    }


def test_position_size_is_capped_by_the_cash():
    size = position_size(100.0, 100.0, 98.0, 0.001, 3_000.0)
    assert size["capped"] is True
    assert size["coins"] == pytest.approx(3_000.0 / (100.0 * 1.001))
    assert size["position_value"] + size["buy_fee"] == pytest.approx(3_000.0)
    assert size["risk_usdt"] < 100.0
    with pytest.raises(Invalid):
        position_size(100.0, 100.0, 100.0, 0.001, 3_000.0)  # stop at the entry
    with pytest.raises(Invalid):
        position_size(100.0, 100.0, 98.0, 0.001, 0.0)       # no cash


def test_atr_is_wilder_smoothed_true_range():
    import pandas as pd
    h, l, c = pd.Series([10, 11, 12, 11, 13.0]), pd.Series([9, 10, 10, 9, 11.0]), pd.Series([9.5, 10.5, 11, 10, 12.0])
    assert core.true_range(h, l, c).tolist() == [1.0, 1.5, 2.0, 2.0, 3.0]
    atr = core.atr(h, l, c, 2).tolist()
    assert atr[0] != atr[0]  # NaN warm-up
    assert atr[1:] == pytest.approx([1.25, 1.625, 1.8125, 2.40625])


# ------------------------------------------------------------------ the clock
def test_trading_day_and_entry_window_in_winter_and_summer():
    six = utc_ms(2024, 1, 15, 5, 0)                    # 06:00 Malta in winter (UTC+1)
    assert trading_day(six) == date(2024, 1, 15)
    start, end = day_bounds(date(2024, 1, 15))
    assert start == six and end == utc_ms(2024, 1, 15, 23, 0)
    assert trading_day(six - 1) == date(2024, 1, 14)   # 05:59:59 still belongs to the day before
    assert in_entry_window(end) and trading_day(end) == date(2024, 1, 15)          # 24:00 counts
    assert not in_entry_window(end + Q) and trading_day(end + Q) == date(2024, 1, 15)  # 00:15: the night
    assert local_ms(date(2024, 7, 15), 6) == utc_ms(2024, 7, 15, 4, 0)             # summer (UTC+2)
    assert day_bounds(date(2024, 7, 15))[1] == utc_ms(2024, 7, 15, 22, 0)


# ----------------------------------------------------------------- the engine
def test_prepare_puts_the_cursor_on_the_first_0600_candle(day: DayEngine, session: dict):
    sid = session["id"]
    assert session["status"] == "ready" and session["base_interval"] == "15m"
    assert session["cursor_ms"] == FIRST_CURSOR
    assert local_hm(session["cursor_ms"] + Q) == (date(2018, 5, 10), 6, 0)
    assert session["price"] == pytest.approx(quarter_candle(FIRST_CURSOR)[3])
    d = session["day"]
    assert d["date"] == "2018-05-10" and d["in_window"] and not d["night"] and d["phase"] == "trading"
    assert d["risk_amount"] == 100.0 and d["timezone"] == "Europe/Malta" and d["utc_offset_min"] == 120
    assert d["check"]["ema_above"] is True and d["check"]["ema200"] is not None and d["check"]["hh_hl"] is None
    assert d["check"]["daily_close_iso"].startswith("2018-05-09")   # yesterday's completed UTC day
    assert d["entries_allowed"] is True and d["block_reason"] is None   # the trend check never blocks
    assert d["can_skip"] is True and d["can_sell"] is False and d["trade"] is None
    assert day.prepare(sid)["cursor_ms"] == FIRST_CURSOR               # re-running keeps the cursor
    assert [x["mode"] for x in day.replay.list_sessions()] == ["day"]
    with pytest.raises(Invalid):   # the bucket controls are not for a day session
        day.replay.step(sid)
    with pytest.raises(Invalid):
        day.replay.buy(sid, 100.0)
    with pytest.raises(Invalid):
        day.replay.reset(sid)
    with pytest.raises(Invalid):   # and a swing session is not a day session
        sw = day.replay.create_session("BTCUSDT", START, END, base_interval="1h")
        day.state(sw["id"])


def test_trend_check_is_saved_but_never_blocks(day: DayEngine, session: dict):
    sid = session["id"]
    st = day.set_trend_check(sid, False)
    assert st["day"]["uptrend"] is False and st["day"]["check"]["hh_hl"] is False
    assert st["day"]["entries_allowed"] is True and st["day"]["block_reason"] is None
    st = day.set_trend_check(sid, True)
    assert st["day"]["uptrend"] is True and st["day"]["entries_allowed"] is True and st["day"]["block_reason"] is None
    with pytest.raises(Invalid):
        day.plan(sid, None)
    with pytest.raises(Invalid):
        day.set_trend_check(sid, "yes")  # type: ignore[arg-type]


def test_plan_and_buy(day: DayEngine, session: dict):
    sid, price = session["id"], session["price"]
    tight = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    p = day.plan(sid, tight["id"])   # before the trend check: no block, only a warning
    assert p["blocked"] is None and p["signals"]["uptrend"] is None and any("not answered" in w for w in p["warnings"])
    day.set_trend_check(sid, True)
    # the default stop is the middle of the zone; here the middle is the price itself, so it is refused
    p = day.plan(sid, tight["id"])
    assert p["stop_suggested"] == pytest.approx(price) and p["errors"] == ["The stop must be below the entry price."]
    assert p["coins"] is None and p["can_buy"] is False
    # a stop less than 0.1 % away asks for more than the cash: the size is capped
    p = day.plan(sid, tight["id"], stop=price - 0.05)
    assert p["capped"] is True and p["position_value"] + p["buy_fee"] == pytest.approx(100_000.0)
    assert p["risk_usdt"] < 100.0 and any("capped" in w for w in p["warnings"])
    z = day.add_zone(sid, "1h", price + 0.5, price - 3.0)   # a wider zone: the full 100 USDT risk fits
    p = day.plan(sid, z["id"])
    assert p["blocked"] is None and p["errors"] == [] and p["entry_price"] == price
    assert p["stop_suggested"] == pytest.approx(price - 1.25)                       # (upper + lower) / 2
    assert p["atr"] > 0 and p["stop_atr"] == pytest.approx(z["lower"] - STOP_ATR_MULT * p["atr"])
    assert p["stop_used"] == p["stop_suggested"] and p["ratio"] is None
    assert p["coins"] == pytest.approx(100.0 / (price - p["stop_used"]))
    assert p["position_value"] == pytest.approx(p["coins"] * price) and p["buy_fee"] == pytest.approx(p["position_value"] * 0.001)
    assert p["risk_usdt"] == pytest.approx(100.0) and p["capped"] is False
    assert p["take_profit"] is None and p["rr"] is None and p["can_buy"] is False
    assert p["signals"]["price_in_zone"] is True and p["signals"]["uptrend"] is True
    assert p["signals"]["rsi"] is not None and p["signals"]["rsi_day_min"] == pytest.approx(p["signals"]["rsi"])  # first candle of the day
    assert p["signals"]["close_above_prev_high"] == (price > quarter_candle(FIRST_CURSOR - Q)[1])
    tp = price + 1.5 * (price - p["stop_used"])
    p2 = day.plan(sid, z["id"], take_profit=tp)
    assert p2["rr"] == pytest.approx(1.5) and any("below 2.0" in w for w in p2["warnings"]) and p2["can_buy"] is True
    # a ratio instead of a take-profit: the target is entry + ratio x (entry - stop), also with a moved stop
    p3 = day.plan(sid, z["id"], ratio=2.0)
    assert p3["take_profit"] == pytest.approx(price + 2.5) and p3["rr"] == pytest.approx(2.0) and p3["ratio"] == 2.0
    assert p3["warnings"] == [] and p3["can_buy"] is True
    p4 = day.plan(sid, z["id"], stop=price - 2.0, ratio=3.0)
    assert p4["stop_used"] == price - 2.0 and p4["take_profit"] == pytest.approx(price + 6.0) and p4["coins"] == pytest.approx(50.0)
    assert day.plan(sid, z["id"], ratio=0.0)["errors"] and day.plan(sid, z["id"], ratio=101.0)["errors"]
    p5 = day.plan(sid, z["id"], take_profit=tp, ratio=3.0)           # an explicit take-profit wins over the ratio
    assert p5["take_profit"] == tp and p5["rr"] == pytest.approx(1.5)
    with pytest.raises(Invalid):
        day.buy(sid, z["id"], take_profit=None, stop=price + 1.0, ratio=2.0)
    with pytest.raises(Invalid):
        day.buy(sid, z["id"], take_profit=price)                   # take-profit must be above the entry
    with pytest.raises(Invalid):
        day.buy(sid, z["id"], take_profit=tp, stop=price + 1.0)    # stop must be below the entry
    with pytest.raises(Invalid):
        day.buy(sid, z["id"], take_profit=None)
    for bad in (-1, 11, 7.5, True):                                # the confidence score is a whole number 0..10
        with pytest.raises(Invalid):
            day.buy(sid, z["id"], take_profit=tp, confidence=bad)
    res = day.buy(sid, z["id"], take_profit=None, ratio=1.5, notes="  first try  ", confidence=8)   # the target from the ratio
    t = res["trade"]
    assert t["status"] == "open" and t["seq"] == 1 and t["entry_price"] == price and t["entry_ms"] == FIRST_CURSOR
    assert t["take_profit"] == pytest.approx(tp) and t["rr"] == pytest.approx(1.5) and t["notes"] == "first try"
    assert t["confidence"] == 8 and day.state(sid)["day"]["trade"]["confidence"] == 8 and day.trades(sid)["trades"][0]["confidence"] == 8
    assert t["coins"] == pytest.approx(p["coins"])
    assert t["ema_above"] is True and t["hh_hl"] is True and t["day_date"] == "2018-05-10" and t["capped"] is False
    assert t["zone_upper"] == z["upper"] and t["zone_lower"] == z["lower"] and t["entry_atr"] == pytest.approx(p["atr"])
    assert t["stop_suggested"] == pytest.approx(price - 1.25) and t["stop_atr"] == pytest.approx(p["stop_atr"]) and t["stop_used"] == t["stop_suggested"]
    assert t["entry_local"] == "2018-05-10 06:00" and t["overnight"] is False
    st = res["state"]
    assert st["cash"] == pytest.approx(100_000 - p["position_value"] - p["buy_fee"])
    assert st["day"]["entries_allowed"] is False and st["day"]["block_reason"] == "A trade is open."
    assert st["day"]["trade"]["id"] == t["id"] and st["account"]["open_trades"] == 1 and st["day"]["can_skip"] is False
    assert st["account"]["equity"] == pytest.approx(st["cash"] + t["coins"] * price)
    with pytest.raises(Conflict):
        day.buy(sid, z["id"], take_profit=tp)
    with pytest.raises(Conflict):
        day.skip_day(sid)


def test_take_profit_fires_on_the_next_candle_and_the_step_after_jumps(db: Path):
    nxt = FIRST_CURSOR + Q
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {nxt: (price, price + 5.0, price - 0.1, price + 1.0)})  # the next candle spikes up
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    day.set_trend_check(sid, True)
    z = day.add_zone(sid, "4h", price + 0.5, price - 0.5)
    res = day.buy(sid, z["id"], take_profit=price + 4.0, stop=price - 2.0)
    t = res["trade"]
    assert t["stop_used"] == price - 2.0 and t["stop_suggested"] != t["stop_used"] and t["coins"] == pytest.approx(50.0)
    st = day.step(sid)
    assert [e["kind"] for e in st["events"]] == ["exit"]
    ev = st["events"][0]
    assert ev["exit_type"] == "take-profit"
    # the replay stays on the exit candle: end the day or keep trading
    assert st["cursor_ms"] == nxt and ev["choice"] is True and ev["jumped_to_ms"] is None
    assert st["day"]["exit_choice"] is True and st["day"]["review_pending"] is None and st["day"]["can_continue"] is False
    assert st["day"]["entries_allowed"] is True    # buying again means keep trading
    # "End the day" jumps to the candle closing at 24:00 of the exit day, for the review
    st = day.end_day(sid)
    assert st["events"][0]["kind"] == "jump" and st["events"][0]["to_local"] == "2018-05-11 00:00"
    assert local_hm(st["cursor_ms"] + Q) == (date(2018, 5, 11), 0, 0) and st["day"]["exit_choice"] is False
    assert st["day"]["date"] == "2018-05-10" and st["day"]["local_time"] == "2018-05-11 00:00"
    with pytest.raises(Conflict):
        day.end_day(sid)    # the day has ended
    tr = day.trades(sid)["trades"][-1]
    assert tr["status"] == "closed" and tr["exit_price"] == price + 4.0 and tr["exit_ms"] == nxt and tr["overnight"] is False
    want = trade_result(price, price + 4.0, tr["coins"], tr["position_value"], tr["buy_fee"], 0.001, 100.0)
    assert tr["pnl"] == pytest.approx(want["pnl"]) and tr["r_multiple"] == pytest.approx(want["r_multiple"])
    assert tr["pnl_pct"] == pytest.approx(want["pnl_pct"]) and tr["fees"] == pytest.approx(tr["buy_fee"] + tr["sell_fee"])
    assert st["cash"] == pytest.approx(100_000 + tr["pnl"]) and st["account"]["realized_pnl"] == pytest.approx(tr["pnl"])
    assert st["day"]["phase"] == "exited" and st["day"]["entries_allowed"] is False   # nothing before the review
    assert st["day"]["block_reason"] == "Answer the review questions for trade #1 first." and st["day"]["can_continue"] is False
    assert st["day"]["last_exit"]["id"] == tr["id"] and st["day"]["review_pending"]["id"] == tr["id"]
    # "what if": the 15-minute candles after the exit candle until 24:00 local of the exit day
    df = day.candles.get_candles("BTCUSDT", "15m", nxt + Q, local_ms(date(2018, 5, 11), 0) - Q)
    assert tr["whatif_n"] == len(df) > 0
    assert tr["whatif_high"] == df["high"].max() and tr["whatif_low"] == df["low"].min()
    exit_price = tr["exit_price"]
    assert tr["whatif"]["above_exit_pct"] == round((tr["whatif_high"] - exit_price) / exit_price * 100, 2)
    assert tr["whatif"]["low_vs_entry_pct"] == round((tr["whatif_low"] - price) / price * 100, 2)
    with pytest.raises(Conflict):
        day.sell(sid, "stalls")
    with pytest.raises(Conflict):
        day.resume(sid)     # the review first
    assert tr["review"]["reviewed"] is False and tr["review"]["over_target_suggested"] is (tr["whatif_high"] >= price + 4.0)
    rv = day.review(sid, tr["id"], {"stop_tight": False, "over_target": True, "stop_saved": False})
    assert rv["trade"]["review"]["over_target"] is True and rv["trade"]["review"]["reviewed"] is True
    assert rv["state"]["day"]["review_pending"] is None and rv["state"]["day"]["can_continue"] is True and rv["state"]["day"]["entries_allowed"] is True
    st2 = day.resume(sid)   # Continue after an exit is the jump to the next 06:00 (a step would go on with the day)
    assert st2["events"][0]["kind"] == "jump" and local_hm(st2["cursor_ms"] + Q) == (date(2018, 5, 11), 6, 0)
    assert st2["day"]["phase"] == "trading" and st2["day"]["date"] == "2018-05-11"
    assert st2["day"]["check"]["hh_hl"] is None and st2["day"]["traded_today"] == 0 and st2["day"]["last_exit"] is None
    rows = day.trades(sid)["trades"]
    assert len(rows) == 1 and rows[0]["exit_type"] == "take-profit" and rows[0]["exit_local"] == "2018-05-10 06:15"


def test_conditions_are_saved_and_evaluated(db: Path):
    """Every trade carries its entry conditions (yes / no / unknown); the evaluation groups the
    closed trades with and without each one."""
    nxt = FIRST_CURSOR + Q
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {nxt: (price, price + 5.0, price - 0.1, price + 1.0)})
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    z = day.add_zone(sid, "4h", price + 0.5, price - 0.5)
    t = day.buy(sid, z["id"], take_profit=price + 4.0, stop=price - 2.0)["trade"]   # no trend check answered
    c = t["conditions"]
    assert t["uptrend"] is None and t["price_in_zone"] is True and t["zone_interval"] == "4h"
    assert c["uptrend"] is None and c["ema_above"] is True and c["hh_hl"] is None
    assert c["price_in_zone"] is True and c["rr_ok"] is True and c["zone_4h"] is True
    assert c["stop_middle"] is False and c["stop_atr"] is False and c["stop_own"] is True
    assert c["morning"] is True and c["afternoon"] is False and c["evening"] is False
    assert c["rsi_dipped"] is t["rsi_dipped"] and c["close_above_prev_high"] is t["close_above_prev_high"]
    assert set(c) == {k for k, _ in CONDITIONS}
    st = day.step(sid)                      # take-profit: the first closed trade (a win)
    assert st["events"][0]["kind"] == "exit"
    day.review(sid, st["events"][0]["trade"]["id"], {"stop_tight": False, "over_target": False, "stop_saved": False})
    st = day.resume(sid)                    # Continue: the jump to the next day
    day.set_trend_check(sid, False)
    st = day.state(sid)
    z2 = day.add_zone(sid, "1h", st["price"] + 0.5, st["price"] - 3.0)
    t2 = day.buy(sid, z2["id"], None, ratio=2.0)["trade"]   # the default stop (zone middle), no uptrend
    assert t2["conditions"]["uptrend"] is False and t2["conditions"]["stop_middle"] is True and t2["conditions"]["stop_own"] is False
    day.sell(sid, "stalls")                 # the second closed trade
    rows = day.trades(sid)["trades"]
    ev = day.conditions(sid)
    assert ev["scope"] == "session" and ev["trades"] == 2 and ev["sessions"] == 1 and ev["all"]["trades"] == 2
    by = {c["key"]: c for c in ev["conditions"]}
    assert [c["key"] for c in ev["conditions"]] == [k for k, _ in CONDITIONS]
    up = by["uptrend"]
    assert up["unknown"] == 1 and up["with"]["trades"] == 0 and up["without"]["trades"] == 1
    assert up["with"]["win_rate"] is None and up["with"]["avg_r"] is None and up["with"]["pnl"] == 0.0
    rr = by["rr_ok"]
    assert rr["with"]["trades"] == 2 and rr["without"]["trades"] == 0 and rr["unknown"] == 0
    assert rr["with"]["wins"] == sum(1 for x in rows if x["pnl"] > 0)
    mid = by["stop_middle"]
    assert mid["with"]["trades"] == 1 and mid["without"]["trades"] == 1
    zone4 = by["zone_4h"]
    assert zone4["with"]["trades"] == 1 and zone4["with"]["wins"] == 1 and zone4["with"]["win_rate"] == 100.0
    assert zone4["with"]["avg_r"] == pytest.approx(rows[0]["r_multiple"]) and zone4["with"]["pnl"] == pytest.approx(rows[0]["pnl"])
    assert ev["all"]["pnl"] == pytest.approx(sum(x["pnl"] for x in rows))
    assert day.conditions(sid, "all")["trades"] == 2
    with pytest.raises(Invalid):
        day.conditions(sid, "week")
    lines = day.conditions_csv(sid).splitlines()
    assert lines[0].startswith("condition,label,with_trades,with_wins") and len(lines) == 1 + len(CONDITIONS)
    assert lines[1].startswith("uptrend,") and lines[1].endswith(",1")


def test_move_stop_and_target_is_tracked(db: Path):
    """The stop and the target of the open trade can be moved; each move is saved, the stop choice and
    the risk keep the levels of the buy, and the moved stop fires at its new price."""
    nxt = FIRST_CURSOR + Q
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {nxt: (price, price + 0.2, price - 1.5, price - 0.3)})   # the next candle dips 1.5 below the entry
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    t = day.buy(sid, z["id"], take_profit=price + 4.0, stop=price - 2.0)["trade"]
    assert t["changes"] == [] and t["stop_initial"] == price - 2.0 and t["tp_initial"] == price + 4.0
    assert t["conditions"]["stop_moved"] is False and t["conditions"]["tp_moved"] is False
    assert t["price_change_pct"] == pytest.approx(0.0) and t["to_stop_pct"] == pytest.approx(-2.0 / price * 100) and t["to_tp_pct"] == pytest.approx(4.0 / price * 100)
    assert t["price_change_usdt"] == pytest.approx(0.0) and t["stop_pct"] == pytest.approx(-2.0 / price * 100) and t["tp_pct"] == pytest.approx(4.0 / price * 100)
    with pytest.raises(Invalid):
        day.move_trade(sid)
    with pytest.raises(Invalid):
        day.move_trade(sid, stop=price)              # not below the price
    with pytest.raises(Invalid):
        day.move_trade(sid, take_profit=price)       # not above the price
    with pytest.raises(Invalid):
        day.move_trade(sid, stop=float("nan"))
    res = day.move_trade(sid, stop=price - 1.0)      # the stop is tightened
    t = res["trade"]
    assert t["stop_used"] == price - 1.0 and t["take_profit"] == price + 4.0 and t["stop_initial"] == price - 2.0
    assert len(t["changes"]) == 1 and t["changes"][0]["field"] == "stop" and t["changes"][0]["from"] == price - 2.0 and t["changes"][0]["to"] == price - 1.0
    assert t["changes"][0]["at_ms"] == FIRST_CURSOR and t["changes"][0]["at_local"].endswith("06:00")
    assert t["conditions"]["stop_moved"] is True and t["conditions"]["stop_own"] is True   # the choice at the buy still counts
    assert t["rr"] == pytest.approx(2.0) and t["rr_now"] == pytest.approx(4.0) and t["stop_pct"] == pytest.approx(-1.0 / price * 100)
    assert res["state"]["day"]["trade"]["stop_used"] == price - 1.0
    same = day.move_trade(sid, stop=price - 1.0, take_profit=price + 4.0)["trade"]   # no change: nothing recorded
    assert len(same["changes"]) == 1
    same = day.move_trade(sid, take_profit=(price + 4.0) * (1 + 1e-12))["trade"]     # float noise is no move either
    assert len(same["changes"]) == 1 and same["take_profit"] == price + 4.0
    t = day.move_trade(sid, take_profit=price + 3.0)["trade"]
    assert [c["field"] for c in t["changes"]] == ["stop", "take_profit"] and t["conditions"]["tp_moved"] is True
    st = day.step(sid)                               # the candle's low reaches the moved stop, not the old one
    ev = st["events"][0]
    assert ev["kind"] == "exit" and ev["exit_type"] == "stop" and ev["trade"]["exit_price"] == price - 1.0
    closed = ev["trade"]
    assert closed["r_multiple"] == pytest.approx(closed["pnl"] / closed["risk_amount"])   # R against the risk at the buy
    assert len(closed["changes"]) == 2 and closed["stop_initial"] == price - 2.0 and closed["price_change_pct"] is None
    with pytest.raises(Conflict):
        day.move_trade(sid, stop=price - 3.0)       # closed
    by = {c["key"]: c for c in day.conditions(sid)["conditions"]}
    assert by["stop_moved"]["with"]["trades"] == 1 and by["tp_moved"]["with"]["trades"] == 1


def test_buying_is_always_allowed(db: Path):
    """Only an open trade (or the end of the replay) blocks a buy: a second trade on the same day,
    one right after an exit and one at night are all allowed; the Days journal counts them all."""
    nxt = FIRST_CURSOR + Q
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {nxt: (price, price + 5.0, price - 0.1, price + 1.0)})
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    z = day.add_zone(sid, "4h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price + 4.0, stop=price - 2.0)
    st = day.step(sid)                                   # take-profit on the next candle
    assert st["events"][0]["kind"] == "exit" and st["day"]["exit_choice"] is True
    tid1 = st["events"][0]["trade"]["id"]
    st = day.end_day(sid)                                # end the day: 24:00, where the review is asked
    assert st["day"]["phase"] == "exited" and st["day"]["local_time"] == "2018-05-11 00:00"
    assert st["day"]["entries_allowed"] is False and "review" in st["day"]["block_reason"] and st["day"]["can_continue"] is False
    with pytest.raises(Conflict):
        day.buy(sid, z["id"], take_profit=price * 3.0, stop=price * 0.5)
    st = day.review(sid, tid1, {"stop_tight": False, "over_target": False, "stop_saved": False})["state"]
    assert st["day"]["entries_allowed"] is True and st["day"]["block_reason"] is None and st["day"]["can_continue"] is True
    p2 = st["price"]
    res = day.buy(sid, z["id"], take_profit=p2 * 3.0, stop=p2 * 0.5)   # the second trade of the day, right after the review (the 24:00 close)
    assert res["trade"]["seq"] == 2 and res["trade"]["day_date"] == "2018-05-10"
    d = res["state"]["day"]
    assert d["phase"] == "trading" and d["traded_today"] == 2 and d["can_sell"] is True and d["can_continue"] is False and d["last_exit"] is None
    assert d["entries_allowed"] is False and d["block_reason"] == "A trade is open."
    st = day.step(sid, 2)                                # into the night with the trade open
    assert st["day"]["night"] is True and st["day"]["trade"]["overnight"] is True and st["day"]["date"] == "2018-05-10"
    st = day.sell(sid, "no_overnight")
    assert st["day"]["phase"] == "exited" and st["day"]["night"] is True and st["events"][0]["jumped_to_ms"] is None   # a night exit: no jump
    tid2 = st["events"][0]["trade"]["id"]
    assert st["day"]["review_pending"]["id"] == tid2 and st["day"]["entries_allowed"] is False
    st = day.review(sid, tid2, {"stop_tight": True, "over_target": False, "stop_saved": False})["state"]
    assert st["day"]["entries_allowed"] is True and st["day"]["night"] is True
    p3 = st["price"]
    res = day.buy(sid, z["id"], take_profit=p3 * 3.0, stop=p3 * 0.5)   # a buy at night
    assert res["trade"]["seq"] == 3 and res["trade"]["day_date"] == "2018-05-10" and res["state"]["day"]["phase"] == "trading"
    rows = day.days(sid)["days"]
    assert rows[0]["date"] == "2018-05-10" and rows[0]["trade_count"] == 3 and rows[0]["trade_seqs"] == [1, 2, 3]
    assert rows[0]["trade"]["seq"] == 3 and rows[0]["outcome"] == "open"
    closed = [x for x in day.trades(sid)["trades"] if x["status"] == "closed"]
    assert rows[0]["day_pnl"] == pytest.approx(sum(x["pnl"] for x in closed)) and len(closed) == 2
    assert rows[0]["exit_types"] == ["take-profit", "early exit"]   # the open third trade has none yet
    assert "trade_count" in rows[0] and day.days_csv(sid).startswith("date,")


def test_stop_wins_when_both_levels_are_hit_in_one_candle(db: Path):
    nxt = FIRST_CURSOR + Q
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {nxt: (price, price + 10.0, price - 10.0, price)})
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    day.set_trend_check(sid, True)
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price + 4.0, stop=price - 2.0)
    st = day.step(sid, 5)   # stops at the exit, the remaining steps are not taken
    assert st["cursor_ms"] == nxt and st["events"][0]["choice"] is True
    tr = st["events"][0]["trade"]
    assert tr["exit_ms"] == nxt
    assert st["events"][0]["exit_type"] == "stop" and tr["exit_price"] == price - 2.0
    want = trade_result(price, price - 2.0, tr["coins"], tr["position_value"], tr["buy_fee"], 0.001, 100.0)
    assert tr["pnl"] == pytest.approx(want["pnl"]) and tr["pnl"] < -100.0 and tr["r_multiple"] == pytest.approx(want["r_multiple"])


# ------------------------------------------------------------ automatic twins
def test_auto_twin_exits_at_the_original_stop_when_the_manual_stop_was_moved(db: Path):
    hit = FIRST_CURSOR + 2 * Q
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {hit: (price, price + 1.0, price - 3.0, price)})   # the low touches the original stop only
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    t = day.buy(sid, z["id"], take_profit=price + 4.0, stop=price - 2.0, confidence=6)["trade"]
    a = day.auto_trades(sid)
    assert a["summary"]["count"] == 1 and a["trades"][0]["status"] == "open" and a["trades"][0]["trade_id"] == t["id"]
    assert a["trades"][0]["stop"] == price - 2.0 and a["trades"][0]["take_profit"] == price + 4.0 and a["trades"][0]["coins"] == t["coins"]
    day.move_trade(sid, stop=price - 5.0)                              # the manual stop goes away, the twin's stays
    st = day.step(sid, 3)
    assert st["day"]["trade"]["status"] == "open" and st["day"]["trade"]["stop_used"] == price - 5.0
    tw = st["day"]["auto"]
    assert tw["status"] == "closed" and tw["exit_type"] == "stop" and tw["exit_price"] == price - 2.0 and tw["exit_ms"] == hit
    want = trade_result(price, price - 2.0, t["coins"], t["position_value"], t["buy_fee"], 0.001, 100.0)
    assert tw["pnl"] == pytest.approx(want["pnl"]) and tw["r_multiple"] == pytest.approx(want["r_multiple"])
    assert tw["manual"]["status"] == "open" and tw["manual"]["moved"] is True and tw["diff"] is None
    assert day.state(sid)["cash"] == pytest.approx(t["cash_after_entry"])   # the twin never touches the cash


def test_auto_twin_reaches_the_target_after_an_early_manual_exit(db: Path):
    hit = FIRST_CURSOR + 3 * Q
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {hit: (price, price + 5.0, price - 1.0, price)})   # the high reaches the target
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price + 4.0, stop=price - 2.0, confidence=6)
    day.step(sid, 1)
    man = day.sell(sid, "other", note="nervous")["events"][0]["trade"]
    day.end_day(sid)
    a = day.auto_trades(sid)                                            # the replay is at 24:00: the twin is settled over the day at once
    tw = a["trades"][0]
    assert tw["status"] == "closed" and tw["exit_type"] == "take-profit" and tw["exit_price"] == price + 4.0 and tw["exit_ms"] == hit
    assert tw["manual"]["exit_type"] == "early exit" and tw["diff"] == pytest.approx(tw["pnl"] - man["pnl"]) and tw["diff"] > 0
    sm = a["summary"]
    assert sm["closed"] == 1 and sm["wins"] == 1 and sm["compared"] == 1 and sm["by_type"] == {"stop": 0, "take-profit": 1}
    assert sm["diff"] == pytest.approx(tw["pnl"] - man["pnl"]) and sm["auto_pnl"] == pytest.approx(tw["pnl"]) and sm["manual_pnl"] == pytest.approx(man["pnl"])


def test_auto_twin_runs_through_the_night_until_the_stop(db: Path):
    hit = FIRST_CURSOR + 80 * Q                                         # 02:00 of the next night
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {hit: (price, price + 1.0, price - 60.0, price)})
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price + 50.0, stop=price - 50.0, confidence=3)
    st = day.step(sid, 72)                                              # the candle closing at 24:00: nothing happened
    tw = st["day"]["auto"]
    assert tw["status"] == "open" and tw["hold_hours"] is None and st["day"]["trade"]["hold_hours"] is None
    st = day.step(sid, 10)                                              # the manual trade (same stop) and the twin stop together
    assert st["events"][0]["exit_type"] == "stop" and st["cursor_ms"] == hit and st["day"]["auto"] is None   # no open trade any more
    tw = day.auto_trades(sid)["trades"][0]
    assert tw["status"] == "closed" and tw["exit_type"] == "stop" and tw["exit_ms"] == hit and tw["hold_hours"] == pytest.approx(20.0)
    assert tw["manual"]["exit_type"] == "stop" and tw["diff"] == pytest.approx(0.0) and st["day"]["last_exit"]["hold_hours"] == pytest.approx(20.0)


def test_auto_twin_survives_jumps_until_the_target(db: Path):
    hit = FIRST_CURSOR + 100 * Q                                        # 07:00 of the next day
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {hit: (price, price + 60.0, price - 1.0, price)})
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price + 50.0, stop=price - 50.0, confidence=3)
    day.step(sid, 2)
    man = day.sell(sid, "other", note="out")["events"][0]["trade"]
    day.review(sid, man["id"], {"stop_tight": False, "over_target": False, "stop_saved": False})
    st = day.resume(sid)                                                # Continue -> next day 06:00: the twin is still open
    tw = day.auto_trades(sid)["trades"][0]
    assert st["day"]["date"] == "2018-05-11" and tw["status"] == "open" and tw["exit_type"] is None
    st = day.step(sid, 4)
    a = day.auto_trades(sid)
    tw = a["trades"][0]
    assert tw["status"] == "closed" and tw["exit_type"] == "take-profit" and tw["exit_ms"] == hit and tw["hold_hours"] == pytest.approx(25.0)
    assert tw["manual"]["hold_hours"] == pytest.approx(0.5) and a["summary"]["avg_hold_hours"] == pytest.approx(25.0)
    assert a["summary"]["by_type"] == {"stop": 0, "take-profit": 1} and a["summary"]["manual_avg_hold_hours"] == pytest.approx(0.5)
    day.reset(sid)
    assert day.auto_trades(sid)["trades"] == []                         # reset wipes the twins too


# ------------------------------------------------------------------- review
def test_review_is_asked_at_2400_and_blocks_the_replay(db: Path):
    """The three review marks are asked at the candle closing at 24:00 of the exit day (here reached
    with "End the day") and the replay waits for them; "over the target" is suggested from the candles
    after the exit. The answers can be changed later, an open trade cannot be reviewed."""
    nxt, later = FIRST_CURSOR + Q, FIRST_CURSOR + 10 * Q
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {nxt: (price, price + 1.0, price - 3.0, price - 1.0),      # the stop on the next candle
                    later: (price, price + 9.0, price - 0.5, price)})        # the target is reached later in the day
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price + 4.0, stop=price - 2.0, confidence=5)
    st = day.step(sid)
    ev = st["events"][0]
    assert ev["exit_type"] == "stop" and ev["choice"] is True and st["day"]["review_pending"] is None
    st = day.end_day(sid)
    tr = st["day"]["review_pending"]
    assert st["day"]["local_time"] == "2018-05-11 00:00" and st["day"]["date"] == "2018-05-10" and st["day"]["phase"] == "exited"
    pend = st["day"]["review_pending"]
    assert pend["id"] == tr["id"] and pend["review"]["reviewed"] is False and pend["review"]["stop_tight"] is None
    assert pend["review"]["over_target_suggested"] is True and tr["whatif_high"] == price + 9.0
    assert [q["key"] for q in st["day"]["review_questions"]] == ["stop_tight", "over_target", "stop_saved"]
    assert st["day"]["can_continue"] is False and st["day"]["entries_allowed"] is False
    assert st["day"]["block_reason"] == "Answer the review questions for trade #1 first."
    for call in (lambda: day.step(sid), lambda: day.step_back(sid), lambda: day.resume(sid), lambda: day.skip_day(sid), lambda: day.goto_day(sid, "2018-05-12"),
                 lambda: day.next_ema_day(sid), lambda: day.buy(sid, z["id"], take_profit=price + 4.0, stop=price - 2.0)):
        with pytest.raises(Conflict):
            call()
    assert local_hm(day.state(sid)["cursor_ms"] + Q) == (date(2018, 5, 11), 0, 0)   # nothing moved
    # all three answers are required, true / false only; unknown trades
    with pytest.raises(Invalid):
        day.review(sid, tr["id"], {"stop_tight": True, "over_target": True})
    with pytest.raises(Invalid):
        day.review(sid, tr["id"], {"stop_tight": 1, "over_target": True, "stop_saved": False})
    with pytest.raises(NotFound):
        day.review(sid, 424242, {"stop_tight": False, "over_target": False, "stop_saved": False})
    res = day.review(sid, tr["id"], {"stop_tight": True, "over_target": True, "stop_saved": False})
    r = res["trade"]["review"]
    assert r["stop_tight"] is True and r["over_target"] is True and r["stop_saved"] is False and r["reviewed"] is True and r["reviewed_at"]
    d = res["state"]["day"]
    assert d["review_pending"] is None and d["can_continue"] is True and d["entries_allowed"] is True and d["block_reason"] is None
    # changed later, also after the day was left; the trades list carries the marks
    st = day.resume(sid)
    assert st["day"]["date"] == "2018-05-11"
    r2 = day.review(sid, tr["id"], {"stop_tight": False, "over_target": True, "stop_saved": True})["trade"]["review"]
    assert r2["stop_tight"] is False and r2["stop_saved"] is True
    assert day.trades(sid)["trades"][0]["review"]["stop_saved"] is True
    # an open trade cannot be reviewed
    p2 = st["price"]
    t2 = day.buy(sid, z["id"], take_profit=p2 + 50.0, stop=p2 - 50.0, confidence=5)["trade"]
    with pytest.raises(Conflict):
        day.review(sid, t2["id"], {"stop_tight": False, "over_target": False, "stop_saved": False})


def test_keep_trading_defers_the_review_to_2400(db: Path):
    """After an exit by day the user may keep trading: the replay stays, a second trade is possible,
    and the review of the first is asked when a step reaches 24:00 of its day (the step stops there)."""
    nxt = FIRST_CURSOR + Q
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {nxt: (price, price + 1.0, price - 3.0, price - 1.0)})    # the stop on the next candle
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price + 4.0, stop=price - 2.0, confidence=5)
    st = day.step(sid)
    t1 = st["events"][0]["trade"]
    assert st["day"]["exit_choice"] is True and st["day"]["can_continue"] is False
    st = day.keep_trading(sid)
    assert st["day"]["phase"] == "trading" and st["day"]["exit_choice"] is False and st["cursor_ms"] == nxt
    assert st["day"]["review_pending"] is None and st["day"]["entries_allowed"] is True
    p2 = st["price"]
    t2 = day.buy(sid, z["id"], take_profit=p2 * 3.0, stop=p2 * 0.5)["trade"]   # the second trade of the day
    assert t2["seq"] == 2
    st = day.step(sid, 200)                                  # stops at the candle closing at 24:00
    assert [e["kind"] for e in st["events"]] == ["review"] and st["events"][0]["trade"]["id"] == t1["id"]
    assert st["day"]["local_time"] == "2018-05-11 00:00" and st["day"]["review_pending"]["id"] == t1["id"]
    assert st["day"]["trade"]["id"] == t2["id"]              # the second trade stays open
    assert day.trades(sid)["trades"][0]["alts"] is not None  # its 24:00 has passed: the alternatives are settled
    with pytest.raises(Conflict):
        day.step(sid)
    day.review(sid, t1["id"], {"stop_tight": False, "over_target": False, "stop_saved": False})
    st = day.step(sid)
    assert st["events"] == [] and st["day"]["night"] is True and st["day"]["trade"]["overnight"] is True


def test_old_trades_without_a_due_review_never_block(db: Path):
    nxt = FIRST_CURSOR + Q
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {nxt: (price, price + 1.0, price - 3.0, price - 1.0)})
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price + 4.0, stop=price - 2.0)
    t = day.step(sid)["events"][0]["trade"]
    with day.store.tx() as conn:
        day.store.update_trade(conn, t["id"], review_due=None)   # a trade from before the rule
    st = day.end_day(sid)
    assert st["day"]["review_pending"] is None and st["day"]["can_continue"] is True
    assert day.step(sid)["events"] == []


def test_step_back_stops_at_the_latest_action(db: Path):
    """Step back goes one 15m candle back, never before the entry, a stop / target move, an exit or
    a jump's landing candle; crossing 06:00 backwards re-opens the day in the journal."""
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db)
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    st = day.state(sid)
    assert st["day"]["can_step_back"] is False              # the first 06:00
    with pytest.raises(Conflict):
        day.step_back(sid)
    day.step(sid, 3)
    st = day.step_back(sid)
    assert st["cursor_ms"] == FIRST_CURSOR + 2 * Q and st["day"]["can_step_back"] is True
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    p = st["price"]
    t = day.buy(sid, z["id"], take_profit=p + 50.0, stop=p - 50.0)["trade"]
    assert day.state(sid)["day"]["can_step_back"] is False  # the entry candle
    day.step(sid, 2)
    day.move_trade(sid, stop=p - 40.0)                      # a move at entry + 2
    day.step(sid, 2)
    st = day.step_back(sid)
    st = day.step_back(sid)
    assert st["cursor_ms"] == t["entry_ms"] + 2 * Q and st["day"]["can_step_back"] is False
    st = day.sell(sid, "stalls")
    assert st["day"]["can_step_back"] is False              # the exit
    day.keep_trading(sid)
    day.step(sid, 2)
    assert day.step_back(sid)["cursor_ms"] == t["entry_ms"] + 3 * Q
    # through the night and over 06:00; back over 06:00 re-opens the day
    st = day.step(sid, 200)
    assert st["events"][0]["kind"] == "review"
    day.review(sid, st["events"][0]["trade"]["id"], {"stop_tight": False, "over_target": False, "stop_saved": False})
    st = day.step(sid, 24)
    assert st["day"]["date"] == "2018-05-11" and day.days(sid)["days"][0]["left_how"] == "exit"
    st = day.step_back(sid)
    assert st["day"]["date"] == "2018-05-10" and st["day"]["night"] is True and day.days(sid)["days"][0]["left_how"] is None
    st = day.step(sid)
    assert st["day"]["date"] == "2018-05-11" and day.days(sid)["days"][0]["left_how"] == "exit"
    # a jump's landing candle is the floor
    st = day.skip_day(sid)
    assert st["day"]["date"] == "2018-05-12" and st["day"]["can_step_back"] is False
    with pytest.raises(Conflict):
        day.step_back(sid)


def test_early_exit_needs_a_reason_and_fills_at_the_close(day: DayEngine, session: dict):
    sid, price = session["id"], session["price"]
    day.set_trend_check(sid, True)
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price + 50.0, stop=price - 50.0)   # far away: nothing fires
    st = day.step(sid, 2)
    assert st["events"] == [] and st["day"]["trade"]["status"] == "open" and st["day"]["trade"]["unrealized_pnl"] is not None
    with pytest.raises(Invalid):
        day.sell(sid, "nonsense")
    with pytest.raises(Invalid):
        day.sell(sid, "other")          # "Other" needs a note
    cursor = st["cursor_ms"]
    st = day.sell(sid, "other", note="  changed my mind ")
    ev = st["events"][0]
    assert ev["kind"] == "exit" and ev["exit_type"] == "early exit"
    tr = ev["trade"]
    assert tr["exit_reason"] == "other" and tr["exit_note"] == "changed my mind" and tr["exit_reason_label"] == "Other"
    assert tr["exit_ms"] == cursor and tr["exit_price"] == pytest.approx(quarter_candle(cursor)[3])
    assert tr["whatif_n"] > 0 and st["day"]["phase"] == "exited"
    assert ev["choice"] is True and st["cursor_ms"] == cursor and st["day"]["review_pending"] is None   # the review waits for 24:00


def test_stepping_runs_through_the_night(day: DayEngine, session: dict):
    sid = session["id"]
    st = day.step(sid, 72)   # 18 hours of 15-minute candles: the candle closing at 24:00
    assert st["events"] == [] and local_hm(st["cursor_ms"] + Q) == (date(2018, 5, 11), 0, 0)
    assert st["day"]["date"] == "2018-05-10" and st["day"]["in_window"] is True and st["day"]["night"] is False
    # an entry at the 24:00 close is still allowed; an exit in that candle has no "what if" window
    day.set_trend_check(sid, True)
    price = st["price"]
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price + 50.0, stop=price - 50.0)
    ev = day.sell(sid, "no_overnight")["events"][0]
    tr = ev["trade"]
    assert tr["whatif_n"] == 0 and tr["whatif"] is None and tr["whatif_above_exit_pct"] is None
    assert ev["jumped_to_ms"] is None and tr["review"]["over_target_suggested"] is False
    with pytest.raises(Conflict):
        day.resume(sid)
    day.review(sid, tr["id"], {"stop_tight": False, "over_target": False, "stop_saved": False})
    st = day.resume(sid)
    assert st["events"][0]["kind"] == "jump" and local_hm(st["cursor_ms"] + Q) == (date(2018, 5, 11), 6, 0)
    assert day.resume(sid)["events"] == []   # idempotent
    # with no trade at all, stepping runs on through the night; 06:00 starts the next day
    st = day.step(sid, 72)
    assert local_hm(st["cursor_ms"] + Q) == (date(2018, 5, 12), 0, 0)
    st = day.step(sid)
    assert st["events"] == [] and local_hm(st["cursor_ms"] + Q) == (date(2018, 5, 12), 0, 15)
    assert st["day"]["date"] == "2018-05-11" and st["day"]["night"] is True and st["day"]["entries_allowed"] is True
    assert st["day"]["leaving_next"] is False
    st = day.step(sid, 22)
    assert local_hm(st["cursor_ms"] + Q) == (date(2018, 5, 12), 5, 45) and st["day"]["leaving_next"] is True
    st = day.step(sid)
    assert st["events"] == [] and local_hm(st["cursor_ms"] + Q) == (date(2018, 5, 12), 6, 0) and st["day"]["date"] == "2018-05-12"
    row = [r for r in day.days(sid)["days"] if r["date"] == "2018-05-11"][0]
    assert row["left_how"] == "end" and row["left_label"] == "No trade"


def test_goto_day_jumps_forward_only(day: DayEngine, session: dict):
    sid = session["id"]
    for bad in ("nonsense", "2018-05-10", "2018-05-09"):          # malformed, the current day, backwards
        with pytest.raises(Invalid):
            day.goto_day(sid, bad)
    with pytest.raises(Invalid):
        day.goto_day(sid, "2018-06-01")                          # after the end of the replay
    st = day.goto_day(sid, "2018-05-13")
    assert st["events"][0]["kind"] == "jump" and "2018-05-13" in st["events"][0]["reason"]
    assert local_hm(st["cursor_ms"] + Q) == (date(2018, 5, 13), 6, 0) and st["day"]["date"] == "2018-05-13"
    rows = day.days(sid)["days"]
    assert [r["date"] for r in rows] == ["2018-05-10", "2018-05-13"]   # the days in between were never seen
    assert rows[0]["left_how"] == "skip" and rows[0]["outcome"] == "skipped" and rows[1]["outcome"] == "current"
    price = st["price"]
    z = day.add_zone(sid, "4h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price * 3.0, stop=price * 0.5)
    with pytest.raises(Conflict):
        day.goto_day(sid, "2018-05-14")                          # a trade is open


def test_next_ema_day(day: DayEngine, session: dict, monkeypatch):
    sid = session["id"]
    st = day.next_ema_day(sid)                      # the fake market is a slow uptrend: the very next day qualifies
    assert st["events"][0]["kind"] == "jump" and st["day"]["date"] == "2018-05-11" and st["day"]["check"]["ema_above"] is True
    real = day._daily_ema
    monkeypatch.setattr(day, "_daily_ema", lambda s: {**real(s), "ema_above": int(trading_day(s["cursor_ms"] + Q) == date(2018, 5, 13))})
    st = day.next_ema_day(sid)                      # the days in between are jumped over
    assert st["day"]["date"] == "2018-05-13" and [r["date"] for r in day.days(sid)["days"]] == ["2018-05-10", "2018-05-11", "2018-05-13"]
    monkeypatch.setattr(day, "_daily_ema", lambda s: {**real(s), "ema_above": 0})
    with pytest.raises(Conflict):
        day.next_ema_day(sid)                       # no such day before the end: nothing moves
    assert day.state(sid)["day"]["date"] == "2018-05-13"
    price = day.state(sid)["price"]
    z = day.add_zone(sid, "4h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price * 3.0, stop=price * 0.5, confidence=5)
    with pytest.raises(Conflict):
        day.next_ema_day(sid)                       # a trade is open


def test_next_signal(day: DayEngine, session: dict, monkeypatch):
    sid = session["id"]
    assert isinstance(day._strategy_signals(day._ready(sid), get_jump_strategy("rline_retest"), validate_params(RLINE_PARAMS, {})), list)
    sig = lambda bar_ms, top: {"bar": 0, "bar_ms": bar_ms, "zone_top": top, "zone_bottom": top - 1.0, "rules": ["A"], "touches": 2,  # noqa: E731
                               "touch_ms": [], "breakout_ms": bar_ms - 5 * H, "psych": None}
    fake = [sig(START - H, 1e6), sig(START + 6 * H, 1e6), sig(START + 6 * H + H, 1e6), sig(START + 3 * DAY, 1e6)]
    monkeypatch.setattr(day, "_strategy_signals", lambda s, js, p: fake)
    st = day.next_signal(sid, "rline_retest")       # the first signal after the cursor, the same day, 08:00 local
    ev = st["events"][0]
    assert st["cursor_ms"] == START + 6 * H and local_hm(st["cursor_ms"] + Q) == (date(2018, 5, 10), 8, 15)
    assert ev["kind"] == "jump" and ev["reason"].startswith("RLine_Retest: retest of") and "night" not in ev["reason"]
    zones = day.zones(sid)
    assert len(zones) == 1 and zones[0]["id"] == ev["zone_id"] and zones[0]["interval"] == "1h" and zones[0]["upper"] == 1e6
    st = day.next_signal(sid, "rline_retest")       # the next hour's signal; the same zone is reused
    assert st["cursor_ms"] == START + 7 * H and len(day.zones(sid)) == 1
    st = day.next_signal(sid, "rline_retest")       # 02:00 local on 2018-05-13: the night of 2018-05-12, still jumped to
    assert st["day"]["date"] == "2018-05-12" and "night" in st["events"][0]["reason"]
    assert [(r["date"], r["left_how"]) for r in day.days(sid)["days"]][0] == ("2018-05-10", "skip")
    with pytest.raises(Conflict):
        day.next_signal(sid, "rline_retest")        # nothing left
    with pytest.raises(Invalid):
        day.next_signal(sid, "nope")
    with pytest.raises(Invalid):
        day.next_signal(sid, "rline_retest", {"min_gap": 0})
    # the signal moment is the first 15m candle of the hour whose low reaches the zone top
    day.reset(sid)
    hour = START + 7 * H                            # the fake's falling half-wave: each 15m low below the one before
    lows = [day.candles.get_candles("BTCUSDT", "15m", hour + k * Q, hour + k * Q)["low"].iloc[0] for k in range(4)]
    assert lows == sorted(lows, reverse=True)
    fake[:] = [{**sig(hour, lows[3] - 1.0), "entry_level": (lows[1] + lows[2]) / 2, "near": True}]   # the near-zone buffer counts
    st = day.next_signal(sid, "rline_retest")
    assert st["cursor_ms"] == hour + 2 * Q and "came within" in st["events"][0]["reason"]
    price = st["price"]
    z = day.add_zone(sid, "4h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price * 3.0, stop=price * 0.5, confidence=5)
    fake[:] = [sig(START + 2 * DAY, 1e6)]
    with pytest.raises(Conflict):
        day.next_signal(sid, "rline_retest")        # a trade is open


def test_default_balance_and_risk(day: DayEngine):
    s = day.create_session("BTCUSDT", START, END)
    assert s["start_balance"] == 100_000 and s["day"]["risk_amount"] == 100.0 and s["day"]["risk_pct"] == 0.1


def test_upgrade_old_risk_sessions(db: Path, day: DayEngine, session: dict):
    sid = session["id"]
    day.set_trend_check(sid, True)
    price = day.state(sid)["price"]
    z = day.add_zone(sid, "1h", price + 0.5, price - 3.0)
    day.buy(sid, z["id"], take_profit=price + 50.0, confidence=5)
    day.step(sid, 3)
    day.sell(sid, "stalls")
    before = day.trades(sid)["trades"][0]
    cash = day.state(sid)["cash"]
    # the same session as the old rule made it: 10k start at 1 % (100 USDT)
    with day.store.tx() as conn:
        conn.execute("UPDATE sessions SET start_balance = 10000, cash = cash - 90000 WHERE id = ?", (sid,))
        conn.execute("UPDATE day_trades SET cash_after_entry = cash_after_entry - 90000, cash_after_exit = cash_after_exit - 90000 WHERE session_id = ?", (sid,))
        conn.execute("UPDATE day_sessions SET risk_pct = 1.0 WHERE session_id = ?", (sid,))
    for _ in range(2):   # the next start upgrades it; the one after changes nothing
        again = make(db)
        st = again.state(sid)
        assert st["start_balance"] == pytest.approx(100_000) and st["cash"] == pytest.approx(cash)
        assert st["day"]["risk_amount"] == 100.0 and st["day"]["risk_pct"] == 0.1
        t = again.trades(sid)["trades"][0]
        assert t["pnl"] == pytest.approx(before["pnl"]) and t["r_multiple"] == pytest.approx(before["r_multiple"])
        with again.store.tx() as conn:
            row = conn.execute("SELECT cash_after_entry, cash_after_exit FROM day_trades WHERE session_id = ?", (sid,)).fetchone()
        assert row[1] == pytest.approx(cash) and row[0] > 90_000


def test_skip_day(day: DayEngine, session: dict):
    sid = session["id"]
    st = day.skip_day(sid)
    assert st["events"][0]["kind"] == "jump" and local_hm(st["cursor_ms"] + Q) == (date(2018, 5, 11), 6, 0)
    assert st["day"]["can_skip"] is True and st["day"]["check"]["hh_hl"] is None


def test_overnight_trade_runs_through_the_night(day: DayEngine, session: dict):
    sid = session["id"]
    st = day.step(sid, 71)   # the candle closing at 23:45
    assert local_hm(st["cursor_ms"] + Q) == (date(2018, 5, 10), 23, 45)
    day.set_trend_check(sid, True)
    price = st["price"]
    z = day.add_zone(sid, "4h", price + 0.5, price - 0.5)
    day.buy(sid, z["id"], take_profit=price + 50.0, stop=price - 50.0)
    st = day.step(sid)   # closes at 24:00: not overnight yet
    assert st["day"]["trade"]["overnight"] is False and st["day"]["night"] is False
    st = day.step(sid)   # closes at 00:15: the night
    assert st["day"]["trade"]["overnight"] is True and st["day"]["night"] is True and st["day"]["date"] == "2018-05-10"
    assert st["day"]["block_reason"] == "A trade is open." and st["day"]["can_sell"] is True   # the night itself does not block
    st = day.step(sid, 4)   # 01:15, still open, still the same trading day
    assert st["events"] == [] and st["day"]["date"] == "2018-05-10"
    tr = day.sell(sid, "no_overnight")["events"][0]["trade"]
    assert tr["overnight"] is True and tr["exit_local"] == "2018-05-11 01:15"
    assert tr["whatif_n"] == 0 and tr["whatif"] is None   # a night exit: nothing of the trading day is left (and no jump)
    assert day.state(sid)["day"]["local_time"] == "2018-05-11 01:15"
    day.review(sid, tr["id"], {"stop_tight": False, "over_target": False, "stop_saved": False})
    st = day.resume(sid)   # the next 06:00 is later the same morning
    assert local_hm(st["cursor_ms"] + Q) == (date(2018, 5, 11), 6, 0) and st["day"]["traded_today"] == 0


def test_ema_not_available_leaves_the_trend_unknown(db: Path):
    day = make(db)
    start = LISTING + 100 * DAY   # a coin with only 100 daily candles before the start
    sid = day.create_session("BTCUSDT", start, start + 3 * DAY)["id"]
    st = day.prepare(sid)
    assert st["start_note"] and "100 daily candles" in st["start_note"]
    assert st["day"]["check"]["ema_above"] is None and st["day"]["check"]["ema200"] is None
    assert st["day"]["check"]["daily_close"] is not None
    st = day.set_trend_check(sid, True)
    assert st["day"]["uptrend"] is None and st["day"]["entries_allowed"] is True and st["day"]["block_reason"] is None
    z = day.add_zone(sid, "1h", st["price"] + 0.5, st["price"] - 3.0)
    p = day.plan(sid, z["id"])
    assert any("EMA 200 not available" in w for w in p["warnings"]) and p["can_buy"] is False   # no take-profit yet
    assert day.plan(sid, z["id"], ratio=2.0)["can_buy"] is True


def test_zones(day: DayEngine, session: dict):
    sid = session["id"]
    with pytest.raises(Invalid):
        day.add_zone(sid, "15m", 1.0, 2.0)     # zones are drawn on 1h and 4h
    with pytest.raises(Invalid):
        day.add_zone(sid, "1h", 5.0, 5.0)
    with pytest.raises(Invalid):
        day.add_zone(sid, "1h", -1.0, 5.0)
    z = day.add_zone(sid, "1h", 90.0, 95.0)    # any click order
    assert (z["upper"], z["lower"], z["interval"]) == (95.0, 90.0, "1h") and z["height_pct"] == pytest.approx(5.5556, rel=1e-3)
    z2 = day.update_zone(sid, z["id"], 100.0, 80.0)
    assert (z2["upper"], z2["lower"]) == (100.0, 80.0)
    assert [x["id"] for x in day.zones(sid)] == [z["id"]]
    day.delete_zone(sid, z["id"])
    assert day.zones(sid) == []
    with pytest.raises(NotFound):
        day.delete_zone(sid, z["id"])
    with pytest.raises(NotFound):
        day.plan(sid, 999)


def test_picture_of_a_trade(day: DayEngine, session: dict):
    sid, price = session["id"], session["price"]
    day.set_trend_check(sid, True)
    z = day.add_zone(sid, "1h", price + 0.5, price - 2.5)   # default stop = the middle = price - 1.0
    tid = day.buy(sid, z["id"], take_profit=price + 4.0)["trade"]["id"]
    with pytest.raises(NotFound):
        day.image_path(sid, tid)
    t = day.attach_image(sid, tid, PNG)
    assert t["has_image"] is True and t["image_path"] == f"{sid}/day_{tid}.png"
    assert day.image_path(sid, tid).read_bytes().startswith(b"\x89PNG")
    with pytest.raises(Invalid):
        day.attach_image(sid, tid, "data:image/png;base64,QUJD")   # not a PNG
    with pytest.raises(NotFound):
        day.attach_image(sid, 999, PNG)


def test_reset_and_delete(day: DayEngine, session: dict):
    sid, price = session["id"], session["price"]
    day.set_trend_check(sid, True)
    z = day.add_zone(sid, "1h", price + 0.5, price - 2.5)   # default stop = the middle = price - 1.0
    tid = day.buy(sid, z["id"], take_profit=price + 4.0)["trade"]["id"]
    day.attach_image(sid, tid, PNG)
    day.step(sid, 3)
    st = day.reset(sid)
    assert st["cursor_ms"] == FIRST_CURSOR and st["cash"] == 100_000 and st["day"]["phase"] == "trading"
    assert day.trades(sid)["trades"] == [] and day.zones(sid) == [] and st["day"]["check"]["hh_hl"] is None
    assert not (day.replay._snapshot_dir() / sid).exists()
    day.delete_session(sid)
    with pytest.raises(NotFound):
        day.state(sid)
    with day.store.tx() as conn:
        for table in ("day_sessions", "day_checks", "day_zones", "day_trades"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (sid,)).fetchone()[0] == 0


# --------------------------------------------------------------- daily journal
def test_daily_journal_tracks_every_day(day: DayEngine, session: dict):
    """Every trading day gets a journal row: trend check, notes, an end-of-day picture and how the
    replay left it (skipped / no trade by 24:00 / trade closed)."""
    sid, price = session["id"], session["price"]
    st = session
    assert st["day"]["leaving_next"] is False and st["day"]["check"]["notes"] is None and st["day"]["check"]["has_image"] is False
    # notes and the picture of the current day; unknown days and overlong notes are refused
    row = day.set_day_notes(sid, "2018-05-10", "  quiet morning  ")
    assert row["notes"] == "quiet morning" and row["outcome"] == "current" and row["trade"] is None
    with pytest.raises(NotFound):
        day.set_day_notes(sid, "2018-05-11", "not seen yet")
    with pytest.raises(Invalid):
        day.set_day_notes(sid, "not-a-date", "x")
    with pytest.raises(Invalid):
        day.set_day_notes(sid, "2018-05-10", "x" * (DAY_NOTES_MAX_CHARS + 1))
    with pytest.raises(NotFound):
        day.day_image_path(sid, "2018-05-10")
    row = day.attach_day_image(sid, "2018-05-10", PNG)
    assert row["has_image"] is True and row["image_local"] == "2018-05-10 06:00"
    assert day.day_image_path(sid, "2018-05-10").read_bytes().startswith(b"\x89PNG")
    assert day.state(sid)["day"]["check"]["notes"] == "quiet morning" and day.state(sid)["day"]["check"]["has_image"] is True
    # day 1 skipped
    st = day.skip_day(sid)
    assert st["day"]["date"] == "2018-05-11"
    d1 = day.days(sid)["days"]
    assert [d["date"] for d in d1] == ["2018-05-10", "2018-05-11"]
    assert d1[0]["outcome"] == "skipped" and d1[0]["left_how"] == "skip" and d1[0]["left_label"] == "Skipped" and d1[0]["left_local"] == "2018-05-10 06:00"
    assert d1[0]["notes"] == "quiet morning" and d1[0]["has_image"] is True
    assert d1[1]["outcome"] == "current" and d1[1]["left_how"] is None
    # day 2: no trade; stepping runs through the night, leaving_next turns on at the 05:45 candle
    for _ in range(100):
        st = day.state(sid)
        if st["day"]["leaving_next"]:
            break
        st = day.step(sid)
        assert st["events"] == []
    assert st["day"]["local_time"] == "2018-05-12 05:45" and st["day"]["date"] == "2018-05-11" and st["day"]["leaving_next"] is True
    st = day.step(sid)
    assert st["events"] == [] and st["day"]["date"] == "2018-05-12" and st["day"]["leaving_next"] is False
    d2 = day.days(sid)["days"][1]
    assert d2["outcome"] == "no trade" and d2["left_how"] == "end" and d2["left_local"] == "2018-05-12 05:45" and d2["notes"] is None
    # day 3: a trade; after the exit leaving_next is on (the continue is the jump), the row shows the trade
    day.set_trend_check(sid, True)
    z = day.add_zone(sid, "1h", price + 0.5, price - 3.0)   # middle = price - 1.25, below the day-3 price
    day.buy(sid, z["id"], take_profit=None, ratio=2.0)
    assert day.state(sid)["day"]["leaving_next"] is False
    st = day.sell(sid, "stalls")
    tid = st["events"][0]["trade"]["id"]
    assert st["day"]["exit_choice"] is True and st["day"]["can_continue"] is False
    st = day.end_day(sid)
    # at 24:00 the review comes first; the night still belongs to the day
    assert st["day"]["leaving_next"] is False and st["day"]["can_continue"] is False and st["day"]["local_time"] == "2018-05-13 00:00"
    d3 = day.days(sid)["days"][2]
    assert d3["outcome"] == "trade" and d3["trade"]["seq"] == 1 and d3["trade"]["exit_type"] == "early exit" and d3["left_how"] is None
    st = day.review(sid, tid, {"stop_tight": False, "over_target": False, "stop_saved": False})["state"]
    assert st["day"]["can_continue"] is True and st["day"]["leaving_next"] is False
    day.resume(sid)
    d3 = day.days(sid)["days"][2]
    assert d3["left_how"] == "exit" and d3["left_label"] == "Trade closed" and d3["trade"]["pnl"] is not None
    rows = day.days(sid)["days"]
    assert [r["date"] for r in rows] == ["2018-05-10", "2018-05-11", "2018-05-12", "2018-05-13"]
    csv_text = day.days_csv(sid)
    lines = csv_text.splitlines()
    assert lines[0].startswith("date,daily_close,ema200,ema_above,hh_hl,uptrend,outcome,left_label") and len(lines) == 5
    assert lines[1].startswith("2018-05-10,") and ",skipped,Skipped,2018-05-10 06:00,,,,,,,True,2018-05-10 06:00,quiet morning" in lines[1]
    assert ",trade,Trade closed," in lines[3] and ",1,early exit,Price stalls at resistance," in lines[3]
    # reset wipes the journal and its pictures
    pic = day.day_image_path(sid, "2018-05-10")
    day.reset(sid)
    assert day.days(sid)["days"][0]["date"] == "2018-05-10" and day.days(sid)["days"][0]["notes"] is None
    assert len(day.days(sid)["days"]) == 1 and not pic.exists()


def test_mae_mfe_and_the_1h_rsi_are_saved_with_the_trade(db: Path):
    """MAE / MFE: the lowest low and the highest high between the entry and the exit against the entry
    price, in percent and in R; the 1-hour RSI at the buy sits next to the 15-minute one. Trades closed
    before the columns existed get MAE / MFE on the next listing."""
    c1, c2, c3 = FIRST_CURSOR + Q, FIRST_CURSOR + 2 * Q, FIRST_CURSOR + 3 * Q
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {c1: (price, price + 1.0, price - 2.5, price),          # the deepest dip
                    c2: (price, price + 3.0, price - 1.0, price + 1.0),    # the highest high
                    c3: (price + 1.0, price + 1.5, price + 0.5, price + 1.0)})
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    plan = day.plan(sid, z["id"], stop=price - 5.0, take_profit=price + 10.0)
    assert plan["signals"]["rsi"] is not None and plan["signals"]["rsi_1h"] is not None
    t = day.buy(sid, z["id"], take_profit=price + 10.0, stop=price - 5.0, confidence=5)["trade"]
    assert t["entry_rsi_1h"] == pytest.approx(plan["signals"]["rsi_1h"]) and t["excursion"] is None
    day.step(sid, 3)
    tr = day.sell(sid, "stalls")["events"][0]["trade"]
    x = tr["excursion"]
    assert x["mae_pct"] == pytest.approx((price - 2.5) / price * 100 - 100) and x["mfe_pct"] == pytest.approx((price + 3.0) / price * 100 - 100)
    assert x["mae_r"] == pytest.approx(-2.5 * tr["coins"] / tr["risk_usdt"]) and x["mfe_r"] == pytest.approx(3.0 * tr["coins"] / tr["risk_usdt"])
    assert x["mae_r"] < 0 < x["mfe_r"]
    # an old trade without the numbers gets them from the candles when the trades are listed
    with day.store.tx() as conn:
        day.store.update_trade(conn, tr["id"], mae_pct=None, mfe_pct=None, mae_r=None, mfe_r=None)
    again = day.trades(sid)["trades"][-1]
    assert again["id"] == tr["id"] and again["excursion"] == pytest.approx(x)


def test_alternative_trades_close_at_24_00_at_the_latest(db: Path):
    """Alt 1 keeps the stop and target of the buy, Alt 2 keeps the stop with a 1:2 target; both end
    with the 24:00 candle when neither level is hit. They appear only once the replay has reached
    that candle, next to the twin in the Auto exits listing, and old trades get them on a listing."""
    c2 = FIRST_CURSOR + 2 * Q
    price = quarter_candle(FIRST_CURSOR)[3]
    day = make(db, {c2: (price, price + 5.0, price - 1.0, price + 1.0)})   # the high reaches Alt 2's target (+4) only
    sid = day.create_session("BTCUSDT", START, END)["id"]
    day.prepare(sid)
    z = day.add_zone(sid, "1h", price + 0.5, price - 0.5)
    t = day.buy(sid, z["id"], take_profit=price + 10.0, stop=price - 2.0, confidence=6)["trade"]
    assert t["alts"] is None
    day.move_trade(sid, stop=price - 1.0)                                  # the alternatives keep the stop of the buy
    day.step(sid, 1)
    assert day.trades(sid)["trades"][0]["alts"] is None                    # still open, 24:00 not reached: nothing yet
    man = day.sell(sid, "other", note="out")["events"][0]["trade"]
    assert man["alts"] is None                                             # 24:00 not reached yet
    day.end_day(sid)                                                       # the replay jumps to 24:00
    man = day.trades(sid)["trades"][0]
    a = man["alts"]
    end = FIRST_CURSOR + 72 * Q                                            # the candle closing at 24:00
    assert a["end_ms"] == end + Q and a["end_local"].endswith("00:00")
    a1, a2 = a["alt1"], a["alt2"]
    assert a1["stop"] == a2["stop"] == price - 2.0 and a1["take_profit"] == price + 10.0 and a2["take_profit"] == price + 4.0
    assert a2["exit_type"] == "take-profit" and a2["exit_ms"] == c2 and a2["exit_price"] == price + 4.0
    want = trade_result(price, price + 4.0, man["coins"], man["position_value"], man["buy_fee"], 0.001, man["risk_amount"])
    assert a2["pnl"] == pytest.approx(want["pnl"]) and a2["r_multiple"] == pytest.approx(want["r_multiple"]) and a2["diff"] == pytest.approx(a2["pnl"] - man["pnl"])
    assert a1["exit_type"] == "end of day" and a1["exit_ms"] == end and a1["exit_price"] == quarter_candle(end)[3] and a1["hold_hours"] == pytest.approx(18.0)
    # the after-exit moves also in R
    w = man["whatif"]
    assert w["above_exit_r"] == pytest.approx((price + 5.0 - man["exit_price"]) * man["coins"] / man["risk_usdt"]) and w["above_exit_r"] > 0
    assert w["below_exit_r"] == pytest.approx((w["low"] - man["exit_price"]) * man["coins"] / man["risk_usdt"]) and w["below_exit_r"] < 0
    # the Auto exits listing carries them with a summary
    auto = day.auto_trades(sid)
    assert auto["trades"][0]["alts"]["alt2"] == a2
    sm = auto["summary"]
    assert sm["alt2"]["count"] == 1 and sm["alt2"]["by_type"]["take-profit"] == 1 and sm["alt2"]["pnl"] == pytest.approx(a2["pnl"])
    assert sm["alt1"]["by_type"]["end of day"] == 1 and sm["alt1"]["diff"] == pytest.approx(a1["pnl"] - man["pnl"]) and sm["alt1"]["compared"] == 1
    # an old trade without them gets them from the candles when the trades are listed
    with day.store.tx() as conn:
        day.store.update_trade(conn, man["id"], alts=None)
    again = day.trades(sid)["trades"][-1]
    assert again["id"] == man["id"] and again["alts"] == a


def test_picture_frame_spans_the_held_days():
    """The trade picture runs from 06:00 of the entry's trading day to 24:00 of the exit's (candle
    open times in seconds: the candles closing at 06:00 and at 24:00); a night exit keeps its candle."""
    def frame(entry_ms, exit_ms):
        f = picture_frame(entry_ms, exit_ms)
        return local_hm(f["from_s"] * 1000 + Q), local_hm(f["to_s"] * 1000 + Q)
    noon, evening = local_ms(date(2018, 5, 10), 12) - Q, local_ms(date(2018, 5, 10), 18) - Q
    assert frame(noon, evening) == ((date(2018, 5, 10), 6, 0), (date(2018, 5, 11), 0, 0))
    next_day = local_ms(date(2018, 5, 11), 10) - Q                       # held overnight
    assert frame(noon, next_day) == ((date(2018, 5, 10), 6, 0), (date(2018, 5, 12), 0, 0))
    night = local_ms(date(2018, 5, 11), 3) - Q                           # closed at 03:00 in the night
    assert frame(noon, night) == ((date(2018, 5, 10), 6, 0), (date(2018, 5, 11), 3, 0))
    night_buy = local_ms(date(2018, 5, 11), 2) - Q                       # bought in the night after the 10th
    assert frame(night_buy, next_day)[0] == (date(2018, 5, 10), 6, 0)
    assert picture_frame(noon, None) is None


def test_alternative_window_ends_at_the_next_24_00_for_night_buys():
    """A buy during the day closes its alternatives at that day's 24:00; a buy at 24:00 itself or in
    the night (00:00-06:00) at the 24:00 of the following trading day."""
    noon = local_ms(date(2018, 5, 10), 12) - Q                           # the candle closing at 12:00
    assert local_hm(alt_window_end(noon)) == (date(2018, 5, 11), 0, 0)
    midnight = local_ms(date(2018, 5, 11), 0) - Q                        # the candle closing at 24:00
    assert local_hm(alt_window_end(midnight)) == (date(2018, 5, 12), 0, 0)
    night = local_ms(date(2018, 5, 11), 2) - Q                           # 02:00 in the night after the 10th
    assert local_hm(alt_window_end(night)) == (date(2018, 5, 12), 0, 0)
    dawn = local_ms(date(2018, 5, 11), 6) - Q                            # the 06:00 candle starts the 11th
    assert local_hm(alt_window_end(dawn)) == (date(2018, 5, 12), 0, 0)


def test_next_signal_keeps_only_the_current_signal_zone(day: DayEngine, session: dict, monkeypatch):
    """Each "Next signal" replaces the zone of the signal before unless a trade used it; leaving the
    day removes the last one too. Hand-drawn zones and resized signal zones are never removed."""
    sid = session["id"]
    sig = lambda bar_ms, top: {"bar": 0, "bar_ms": bar_ms, "zone_top": top, "zone_bottom": top - 1.0, "rules": ["A"], "touches": 2,  # noqa: E731
                               "touch_ms": [], "breakout_ms": bar_ms - 5 * H, "psych": None}
    fake = [sig(START + 6 * H, 1e6), sig(START + 7 * H, 2e6), sig(START + DAY + 6 * H, 3e6)]
    monkeypatch.setattr(day, "_strategy_signals", lambda s, js, p: fake)
    own = day.add_zone(sid, "4h", 500.0, 400.0)                       # drawn by hand
    first = day.next_signal(sid, "rline_retest")["events"][0]["zone_id"]
    second = day.next_signal(sid, "rline_retest")["events"][0]["zone_id"]
    ids = {z["id"]: z for z in day.zones(sid)}
    assert set(ids) == {own["id"], second} and ids[second]["source"] == "signal" and ids[own["id"]]["source"] is None
    price = day.state(sid)["price"]
    day.buy(sid, second, take_profit=price * 3.0, stop=price * 0.5, confidence=5)   # a trade uses the second zone
    day.sell(sid, "other", note="out")
    day.review(sid, day.trades(sid)["trades"][-1]["id"], {"stop_tight": False, "over_target": False, "stop_saved": False})
    third = day.next_signal(sid, "rline_retest")["events"][0]["zone_id"]
    assert {z["id"] for z in day.zones(sid)} == {own["id"], second, third}
    day.update_zone(sid, third, 3e6 + 1.0, 3e6 - 2.0)                 # resized: now the trader's own
    day.skip_day(sid)
    assert {z["id"] for z in day.zones(sid)} == {own["id"], second, third}
    assert first not in {z["id"] for z in day.zones(sid)}
