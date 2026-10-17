"""Strategy tests on synthetic series with known crossings."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.indicators import core
from app.strategies.base import events_from_state, sign_state
from app.strategies.builtin import STRATEGIES, get_strategy


def df_from(closes) -> pd.DataFrame:
    c = pd.Series(np.asarray(closes, dtype=float))
    return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1.0})


def test_events_from_state():
    state = pd.Series([0, 0, 1, 1, 0, -1, -1, 1, -1, np.nan])
    ev = events_from_state(state)
    assert ev["state"].tolist() == [0, 0, 1, 1, 0, -1, -1, 1, -1, 0]
    assert ev["enter"].tolist() == [False, False, True, False, False, False, False, True, False, False]
    assert ev["exit"].tolist() == [False, False, False, False, False, True, False, False, True, False]


def test_sign_state():
    s = sign_state(pd.Series([1.5, -0.1, 0.0, np.nan, 2.0]))
    assert s.tolist() == [1, -1, 0, 0, 1]


def test_all_strategies_registered_and_describable():
    assert set(STRATEGIES) == {"rsi_reversal", "ema_cross", "macd_cross", "bb_bounce"}
    for spec in STRATEGIES.values():
        d = spec.describe()
        assert d["key"] == spec.key and d["params"] and d["indicators"]
        for ind in d["indicators"]:
            assert set(ind) == {"key", "params"}
        # Templates let a client resolve the indicator instances for custom parameters.
        custom = {p["name"]: (p["default"] + 3 if p["type"] in ("int", "float") else p["default"]) for p in d["params"]}
        resolved = [{"key": t["key"], "params": {k: custom[v] for k, v in t["params"].items()}} for t in d["indicator_templates"]]
        assert resolved == spec.instances(custom)
        assert resolved != d["indicators"]


def test_rsi_reversal_levels():
    # Fall hard, then rise hard: RSI goes to 0 then back up above 70.
    closes = list(np.linspace(100, 50, 30)) + list(np.linspace(50, 120, 40))
    df = df_from(closes)
    params, sig = get_strategy("rsi_reversal").run(df, {"period": 14})
    assert params == {"period": 14, "oversold": 30.0, "overbought": 70.0}
    assert (sig["state"].iloc[:14] == 0).all()  # warm-up
    assert sig["state"].iloc[14] == 1 and sig["enter"].iloc[14]
    assert sig["enter"].sum() == 1  # stays green, event fires once
    assert sig["state"].iloc[-1] == -1
    assert sig["exit"].sum() == 1
    first_red = sig.index[sig["exit"]][0]
    assert (sig["state"].iloc[first_red:] == -1).all()
    assert (sig.loc[sig["state"] == 1, "rsi"] < 30).all()
    assert (sig.loc[sig["state"] == -1, "rsi"] > 70).all()


def test_rsi_reversal_rejects_inverted_levels():
    with pytest.raises(ValueError):
        get_strategy("rsi_reversal").run(df_from(np.arange(50.0)), {"oversold": 70, "overbought": 30})


def test_ema_cross_events_match_indicator_values():
    rng = np.random.default_rng(3)
    df = df_from(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 400))))
    _, sig = get_strategy("ema_cross").run(df, {"fast": 5, "slow": 20})
    fast = core.ema(df["close"], 5)
    slow = core.ema(df["close"], 20)
    expected = sign_state(fast - slow)
    assert sig["state"].tolist() == expected.tolist()
    assert (sig["state"].iloc[:19] == 0).all()
    # Every enter event is a bar where fast > slow and the previous bar was not green.
    for i in sig.index[sig["enter"]]:
        assert fast[i] > slow[i] and (i == 0 or sig["state"].iloc[i - 1] != 1)
    # There is at least one cross in 400 noisy bars, and events never coincide.
    assert sig["enter"].sum() >= 1 and sig["exit"].sum() >= 1
    assert not (sig["enter"] & sig["exit"]).any()


def test_ema_cross_rejects_fast_ge_slow():
    with pytest.raises(ValueError):
        get_strategy("ema_cross").run(df_from(np.arange(50.0)), {"fast": 20, "slow": 20})


def test_macd_cross_on_sine_wave():
    # Period 120 bars. MACD follows the slope, so it peaks near the steepest
    # rise (t = 0, 120, 240) and bottoms near the steepest fall (t = 60, 180),
    # each with a lag of roughly 20 bars. MACD crosses its signal line shortly
    # after those extremes: up-crosses near 80 and 200, down-crosses near 140.
    t = np.arange(240)
    df = df_from(100 + 30 * np.sin(2 * np.pi * t / 120))
    _, sig = get_strategy("macd_cross").run(df, {})
    assert (sig["state"].iloc[:33] == 0).all()  # 26 + 9 - 1 warm-up bars
    exits = sig.index[sig["exit"]].tolist()
    enters = sig.index[sig["enter"]].tolist()
    assert len(enters) == 2 and 61 <= enters[0] <= 100 and 181 <= enters[1] <= 220, enters
    down = [e for e in exits if e > 60]
    assert len(down) == 1 and 121 <= down[0] <= 160, exits
    assert sig["state"].iloc[110] == 1 and sig["state"].iloc[170] == -1
    assert sig.loc[sig["state"] == 1, "macd"].gt(sig.loc[sig["state"] == 1, "signal"]).all()
    assert sig.loc[sig["state"] == -1, "macd"].lt(sig.loc[sig["state"] == -1, "signal"]).all()


def test_macd_cross_is_silent_on_linear_ramp():
    """On a straight line MACD equals its signal exactly; float noise must not produce crosses."""
    df = df_from(np.linspace(100, 160, 120))
    _, sig = get_strategy("macd_cross").run(df, {})
    assert (sig["state"] == 0).all()
    assert not sig["enter"].any() and not sig["exit"].any()


def test_sign_state_tolerance():
    ref = pd.Series([100.0, 100.0, 100.0])
    diff = pd.Series([1e-12, -1e-12, 1e-3])
    assert sign_state(diff, ref=ref).tolist() == [0, 0, 1]
    assert sign_state(diff).tolist() == [1, -1, 1]


def test_bb_bounce_upper_and_middle_exits():
    # Flat with tiny noise, then a crash below the lower band, then a spike above the upper band.
    rng = np.random.default_rng(1)
    closes = list(100 + rng.normal(0, 0.1, 40)) + [90.0] + [100.0] * 5 + [115.0]
    df = df_from(closes)
    _, sig = get_strategy("bb_bounce").run(df, {"period": 20, "mult": 2.0})
    assert sig["state"].iloc[40] == 1 and sig["enter"].iloc[40]
    assert sig["state"].iloc[-1] == -1 and sig["exit"].iloc[-1]
    assert (sig["state"].iloc[41:46] == 0).all()  # back inside the bands: neutral with "upper" exit
    _, sig_mid = get_strategy("bb_bounce").run(df, {"period": 20, "mult": 2.0, "exit_level": "middle"})
    assert sig_mid["state"].iloc[40] == 1
    # After the crash the middle band is below 100, so 100 closes are at/above the middle: red.
    assert (sig_mid["state"].iloc[41:46] == -1).all()
    with pytest.raises(ValueError):
        get_strategy("bb_bounce").run(df, {"exit_level": "lower"})


def test_signals_use_only_past_data():
    """Appending future bars must not change any earlier signal (no look-ahead)."""
    rng = np.random.default_rng(11)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 300)))
    for key in STRATEGIES:
        _, full = get_strategy(key).run(df_from(closes), {})
        _, part = get_strategy(key).run(df_from(closes[:200]), {})
        for col in ("state", "enter", "exit"):
            assert full[col].iloc[:200].tolist() == part[col].tolist(), f"{key}.{col} changed with future data"
