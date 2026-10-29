"""Indicator tests: hand-computed cases, an external reference series, and
independent loop implementations compared against the vectorised code."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.indicators import core
from app.indicators.registry import INDICATORS, ParamSpec, compute_indicator, validate_params

# StockCharts "Relative Strength Index (RSI)" worked example (QQQQ, 14-period).
STOCKCHARTS_CLOSES = [
    44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
    45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439,
    46.2122, 46.2521, 45.7137, 46.4515, 45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672,
    43.4205, 42.6628, 43.1314,
]
STOCKCHARTS_RSI14 = [  # starts at index 14
    70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38,
    54.71, 50.42, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77,
]


def random_closes(n: int = 600, seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))))


# ------------------------------------------------------------ loop references
def ref_ema(x: list[float], n: int) -> list[float]:
    out = [math.nan] * len(x)
    if len(x) < n:
        return out
    a = 2 / (n + 1)
    y = sum(x[:n]) / n
    out[n - 1] = y
    for i in range(n, len(x)):
        y = a * x[i] + (1 - a) * y
        out[i] = y
    return out


def ref_rsi(c: list[float], n: int) -> list[float]:
    out = [math.nan] * len(c)
    gains = [max(c[i] - c[i - 1], 0.0) for i in range(1, len(c))]
    losses = [max(c[i - 1] - c[i], 0.0) for i in range(1, len(c))]
    if len(gains) < n:
        return out
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def assert_close(actual: pd.Series, expected: list[float], tol: float = 1e-9) -> None:
    assert len(actual) == len(expected)
    for i, (a, e) in enumerate(zip(actual.tolist(), expected)):
        if math.isnan(e):
            assert math.isnan(a), f"index {i}: expected NaN, got {a}"
        else:
            assert abs(a - e) <= tol, f"index {i}: {a} != {e}"


# ---------------------------------------------------------------------- tests
def test_sma_hand_computed():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert_close(core.sma(s, 3), [math.nan, math.nan, 2, 3, 4])


def test_ema_hand_computed():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    # seed = SMA(1,2,3) = 2; alpha = 0.5 -> 3, 4
    assert_close(core.ema(s, 3), [math.nan, math.nan, 2, 3, 4])


def test_ema_matches_loop_reference():
    s = random_closes()
    for n in (1, 2, 12, 26, 200):
        assert_close(core.ema(s, n), ref_ema(s.tolist(), n))


def test_ema_handles_leading_nans():
    s = pd.Series([math.nan, math.nan, 1, 2, 3, 4, 5], dtype=float)
    assert_close(core.ema(s, 3), [math.nan] * 4 + [2, 3, 4])


def test_ema_rejects_interior_nan():
    with pytest.raises(ValueError):
        core.ema(pd.Series([1, 2, math.nan, 4, 5], dtype=float), 2)


def test_rsi_matches_stockcharts_reference():
    r = core.rsi(pd.Series(STOCKCHARTS_CLOSES), 14)
    assert r.iloc[:14].isna().all()
    for i, expected in enumerate(STOCKCHARTS_RSI14):
        assert abs(r.iloc[14 + i] - expected) < 0.01, f"bar {14 + i}: {r.iloc[14 + i]:.4f} vs {expected}"


def test_rsi_matches_loop_reference():
    s = random_closes()
    for n in (2, 7, 14, 50):
        assert_close(core.rsi(s, n), ref_rsi(s.tolist(), n))


def test_rsi_extremes():
    rising = pd.Series(np.arange(1, 40, dtype=float))
    falling = pd.Series(np.arange(40, 1, -1, dtype=float))
    flat = pd.Series(np.full(40, 5.0))
    assert (core.rsi(rising, 14).dropna() == 100).all()
    assert (core.rsi(falling, 14).dropna() == 0).all()
    assert (core.rsi(flat, 14).dropna() == 100).all()  # TradingView convention
    r = core.rsi(random_closes(), 14).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_macd_structure_and_reference():
    s = random_closes()
    m = core.macd(s, 12, 26, 9)
    line = pd.Series(ref_ema(s.tolist(), 12)) - pd.Series(ref_ema(s.tolist(), 26))
    assert_close(m["macd"], line.tolist())
    # Signal EMA is seeded from the first 9 valid MACD values (index 25..33).
    valid = line.iloc[25:].tolist()
    sig = [math.nan] * 25 + ref_ema(valid, 9)
    assert_close(m["signal"], sig)
    assert m["signal"].first_valid_index() == 33
    hist = (m["macd"] - m["signal"]).tolist()
    assert_close(m["hist"], hist)
    with pytest.raises(ValueError):
        core.macd(s, 26, 12, 9)


def test_bollinger_hand_computed_and_properties():
    s = pd.Series([2, 4, 4, 4, 5, 5, 7, 9], dtype=float)  # population std of all 8 = 2, mean 5
    b = core.bollinger(s, 8, 2.0)
    assert b.iloc[:7].isna().all().all()
    assert b["middle"].iloc[-1] == 5.0
    assert b["upper"].iloc[-1] == 9.0
    assert b["lower"].iloc[-1] == 1.0
    b = core.bollinger(random_closes(), 20, 2.0).dropna()
    assert (b["lower"] <= b["middle"]).all() and (b["middle"] <= b["upper"]).all()
    with pytest.raises(ValueError):
        core.bollinger(s, 8, 0)


def test_period_validation():
    s = random_closes(50)
    for bad in (0, -1, 2.5, True, "3"):
        with pytest.raises(ValueError):
            core.sma(s, bad)  # type: ignore[arg-type]
    assert core.sma(s, 60).isna().all()  # longer than the series: all NaN, no crash


# ------------------------------------------------------------------ registry
def test_registry_describes_all_indicators():
    assert set(INDICATORS) == {"sma", "ema", "rsi", "macd", "bbands", "atr", "vdelta"}
    for spec in INDICATORS.values():
        d = spec.describe()
        assert d["key"] == spec.key and d["outputs"] and (d["params"] or spec.key == "vdelta")


def test_volume_delta_is_taker_buy_minus_taker_sell():
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0], "volume": [10.0, 4.0, 6.0], "taker_buy": [7.0, 1.0, float("nan")]})
    params, out = compute_indicator(df, "vdelta", {})
    assert params == {} and list(out.columns) == ["delta"]
    assert out["delta"].iloc[:2].tolist() == [4.0, -2.0]   # 7 bought - 3 sold; 1 bought - 3 sold
    assert math.isnan(out["delta"].iloc[2])                 # no taker-buy volume stored: no bar
    _, out = compute_indicator(df.drop(columns="taker_buy"), "vdelta", {})   # e.g. an aggregated context frame
    assert out["delta"].isna().all()


def test_registry_compute_defaults_and_params():
    df = pd.DataFrame({"close": random_closes()})
    params, out = compute_indicator(df, "ema", {})
    assert params == {"period": 20}
    assert list(out.columns) == ["ema"]
    params, out = compute_indicator(df, "ema", {"period": "50"})  # strings from query params are coerced
    assert params == {"period": 50} and out["ema"].first_valid_index() == 49
    params, out = compute_indicator(df, "macd", {"fast": 5, "slow": 10, "signal": 3})
    assert list(out.columns) == ["macd", "signal", "hist"]


@pytest.mark.parametrize(
    "key,params",
    [
        ("ema", {"period": 0}),
        ("ema", {"period": 2.5}),
        ("ema", {"period": 20, "bogus": 1}),
        ("ema", {"period": True}),
        ("bbands", {"mult": -1}),
        ("macd", {"fast": 30, "slow": 20}),
        ("nope", {}),
    ],
)
def test_registry_rejects_bad_params(key, params):
    df = pd.DataFrame({"close": random_closes(100)})
    with pytest.raises(ValueError):
        compute_indicator(df, key, params)


def test_choice_param():
    spec = (ParamSpec("mode", "Mode", "choice", "a", choices=("a", "b")),)
    assert validate_params(spec, {"mode": "b"}) == {"mode": "b"}
    with pytest.raises(ValueError):
        validate_params(spec, {"mode": "c"})
