"""RLine_Retest: zones from swing highs, the three RLine rules, breakout and retest, no lookahead."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.indicators import core
from app.indicators.registry import validate_params
from app.strategies.rline import RLINE_PARAMS, Touch, Zone, detect_rline, get_jump_strategy, near_round, round_levels

H_S = 3600


def path(start: float, legs: list[tuple[float, int]], flat: int = 20, wick: float = 0.2) -> pd.DataFrame:
    """1h candles walking in straight lines: `flat` bars at `start`, then each (target, bars) leg.
    Each candle opens at the previous close; high / low are `wick` beyond the body."""
    closes = [start] * flat
    for target, bars in legs:
        a = closes[-1]
        closes += list(np.linspace(a, target, bars + 1)[1:])
    c = np.array(closes)
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({
        "time": np.arange(len(c), dtype="int64") * H_S,
        "open": o, "high": np.maximum(o, c) + wick, "low": np.minimum(o, c) - wick, "close": c,
        "volume": np.ones(len(c)),
    })


def bar_at(df: pd.DataFrame, cond) -> int:
    return int(np.flatnonzero(cond(df))[0])


# two rejections at 93.3 (no round number near: 90 / 95), break, move away, come back
TWO_TOUCHES = [(93.3, 10), (88.0, 10), (93.3, 10), (88.0, 10), (97.0, 10), (99.0, 6), (92.0, 10)]
ONE_TOUCH = [(93.3, 10), (88.0, 10), (97.0, 10), (99.0, 6), (92.0, 10)]


def test_round_levels():
    assert round_levels(83_000) == (80_000, 85_000)
    assert round_levels(140) == (100, 150)
    assert round_levels(2_400) == (2_000, 2_500)
    assert round_levels(83_000, half=False) == (80_000, 90_000)
    assert near_round(99_800, 100_300, 0) == 100_000
    assert near_round(93.2, 93.5, 0.3) is None
    assert near_round(99.9, 100.2, 0.0) == 100


def test_rule_a_two_touches_break_and_retest():
    df = path(88.0, TWO_TOUCHES)
    out = detect_rline(df)
    assert len(out["signals"]) == 1
    s = out["signals"][0]
    assert s["rules"] == ["A"] and s["touches"] == 2
    assert s["zone_top"] == pytest.approx(93.5) and s["zone_bottom"] < s["zone_top"]
    brk = bar_at(df, lambda d: (d["close"] > s["zone_top"]) & (d.index > 50))
    assert s["breakout_ms"] == brk * H_S * 1000
    # the signal is the first later bar whose low reaches the top again
    exp = bar_at(df, lambda d: (d["low"] <= s["zone_top"]) & (d.index > brk + 2))
    assert s["bar"] == exp and s["bar_ms"] == exp * H_S * 1000


def test_one_plain_touch_is_no_rline():
    assert detect_rline(path(88.0, ONE_TOUCH))["signals"] == []


def test_rule_b_one_touch_with_high_volume():
    df = path(88.0, ONE_TOUCH)
    peak = int(df["high"].iloc[:40].idxmax())
    df.loc[peak, "volume"] = 3.0
    out = detect_rline(df)
    assert [s["rules"] for s in out["signals"]] == [["B"]]
    df.loc[peak, "volume"] = 1.8                      # below 2 x the average
    assert detect_rline(df)["signals"] == []
    assert [s["rules"] for s in detect_rline(df, {"vol_mult": 1.5})["signals"]] == [["B"]]


def test_rule_c_one_touch_at_a_round_number():
    df = path(95.0, [(100.0, 10), (95.0, 10), (104.0, 10), (106.0, 6), (99.0, 10)])
    out = detect_rline(df)
    assert len(out["signals"]) == 1 and out["signals"][0]["rules"] == ["C"] and out["signals"][0]["psych"] == 100


def test_zone_is_drawn_from_the_wick_to_the_body():
    for spike in (None, 94.0):           # the path's 0.2 wick (below the floor), then a 0.7 wick
        df = path(88.0, ONE_TOUCH)
        hs = df["high"].iloc[:40]
        k = int(hs[hs == hs.max()].index[-1])   # the swing high: the later of two equal highs at the turn
        if spike is not None:
            df.loc[k, "high"] = spike
        df.loc[k, "volume"] = 3.0        # a high-volume touch: an RLine with one touch
        atr = float(core.atr(df["high"], df["low"], df["close"], 14).iloc[k])
        top, body = float(df["high"].iloc[k]), float(max(df["open"].iloc[k], df["close"].iloc[k]))
        s = detect_rline(df)["signals"][0]
        assert s["zone_top"] == pytest.approx(top)
        assert s["zone_top"] - s["zone_bottom"] == pytest.approx(min(max(top - body, 0.5 * atr), 1.0 * atr))
    assert top - body == pytest.approx(0.7) and 0.5 * atr < 0.7 < atr   # the second case is the wick itself


def test_zone_clamp_keeps_every_touch():
    p = validate_params(RLINE_PARAMS, {})
    # the 2023-02-03 / 02-04 BTC case: the latest touch came in a quieter hour (ATR 103)
    z = Zone(touches=[Touch(0, 5, 23715.7, 23604.6, 177.9, 1.0), Touch(21, 26, 23587.8, 23503.7, 103.4, 1.0)])
    z.resize(p)
    assert z.top == 23715.7 and z.bottom == pytest.approx(23715.7 - 177.9)     # 1 ATR of the most volatile touch
    z = Zone(touches=[Touch(0, 5, 100.0, 99.9, 1.0, 1.0), Touch(9, 14, 98.8, 98.7, 1.0, 1.0)])
    z.resize(p)
    assert z.bottom == 98.8                                                     # past 1 ATR, down to the lower touch high


def test_retest_near_the_zone():
    base = TWO_TOUCHES[:-1]              # up to after the breakout, no pullback yet
    z = [x for x in detect_rline(path(88.0, base))["zones"] if x["state"] == "broken"][0]
    df0 = path(88.0, base)
    brk = int(z["breakout_ms"] // 1000 // H_S)
    atr = float(core.atr(df0["high"], df0["low"], df0["close"], 14).iloc[brk])
    stop_at = z["top"] + 0.15 * atr      # the pullback's lowest low: above the top, inside 0.25 ATR
    df = path(88.0, base + [(stop_at + 0.2, 10)])
    assert df["low"].iloc[-12:].min() == pytest.approx(stop_at)
    sig = detect_rline(df)["signals"]
    assert len(sig) == 1 and sig[0]["near"] is True and sig[0]["entry_level"] == pytest.approx(z["top"] + 0.25 * atr)
    assert "came within" in get_jump_strategy("rline_retest").describe_signal(sig[0])
    assert detect_rline(df, {"near_atr": 0.0})["signals"] == []
    assert detect_rline(path(88.0, TWO_TOUCHES))["signals"][0]["near"] is False   # this one dips into the zone


def test_retest_needs_a_close_away():
    # breaks out just above the top, a wick spikes 1 ATR above it, then back to the zone
    df = path(88.0, TWO_TOUCHES[:4] + [(93.8, 2), (93.8, 4), (92.0, 6)])
    brk = bar_at(df, lambda d: (d["close"] > 93.5) & (d.index > 50))
    atr = float(core.atr(df["high"], df["low"], df["close"], 14).iloc[brk])
    assert df["close"].iloc[brk:brk + 6].max() < 93.5 + 0.75 * atr     # no close far enough above
    df.loc[brk + 2, "high"] = 93.5 + 1.0 * atr
    assert detect_rline(df)["signals"] == []                            # a wick alone does not count
    df.loc[brk + 2, "close"] = 93.5 + 0.8 * atr                         # one close 0.8 ATR above
    df.loc[brk + 2, "high"] = df.loc[brk + 2, "close"] + 0.2
    sig = detect_rline(df)["signals"]
    assert len(sig) == 1 and sig[0]["breakout_ms"] == brk * H_S * 1000 and sig[0]["bar"] > brk + 2
    assert detect_rline(df, {"away_atr": 1.0})["signals"] == []         # not 1 ATR, though


def test_late_retest_and_failed_breakout():
    df = path(88.0, TWO_TOUCHES)
    assert detect_rline(df, {"retest_max": 8})["signals"] == []
    # breaks out, never moves away, falls back below the zone: no signal
    df2 = path(88.0, TWO_TOUCHES[:4] + [(93.7, 10), (88.0, 6), (97.0, 10), (92.0, 10)])
    sig = detect_rline(df2)["signals"]
    assert all(s["breakout_ms"] > 60 * H_S * 1000 for s in sig)   # only the later, second breakout could signal


def test_touches_come_from_swing_highs_only():
    # two swing lows at 93.3 (the highs 97.2 and 98.7 between them are single, plain touches),
    # then a rally through everything and back: lows build no zone, so there is no signal
    df = path(92.0, [(97.0, 10), (93.3, 10), (98.5, 10), (93.3, 10), (101.0, 10), (102.5, 6), (92.0, 10)])
    assert detect_rline(df)["signals"] == []


def test_no_lookahead():
    df = path(88.0, TWO_TOUCHES + [(105.0, 20), (100.0, 10), (110.0, 15), (103.0, 10)])
    full = detect_rline(df)["signals"]
    assert full
    for k in range(10, len(df) + 1, 3):
        assert detect_rline(df.iloc[:k])["signals"] == [s for s in full if s["bar"] < k]


def test_params_are_validated():
    with pytest.raises(ValueError):
        detect_rline(path(88.0, ONE_TOUCH), {"min_h_atr": 2.0, "max_h_atr": 1.0})
    with pytest.raises(ValueError):
        detect_rline(path(88.0, ONE_TOUCH), {"nope": 1})
    with pytest.raises(ValueError):
        detect_rline(path(88.0, ONE_TOUCH), {"near_atr": 0.5, "away_atr": 0.5})   # back as far as away
    with pytest.raises(ValueError):
        get_jump_strategy("nope")
    assert get_jump_strategy("rline_retest").describe()["name"] == "RLine_Retest"
    assert detect_rline(path(88.0, [])[:0])["signals"] == []
