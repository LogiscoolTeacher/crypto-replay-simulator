"""The four built-in strategies agreed for version 1."""
from __future__ import annotations

from typing import Any

import pandas as pd

from ..indicators import core
from ..indicators.registry import ParamSpec
from .base import IndicatorTemplate, StrategySpec, events_from_state, sign_state

STRATEGIES: dict[str, StrategySpec] = {}


def _register(spec: StrategySpec) -> StrategySpec:
    STRATEGIES[spec.key] = spec
    return spec


# ------------------------------------------------------------- RSI reversal
def _rsi_check(p: dict[str, Any]) -> None:
    if p["oversold"] >= p["overbought"]:
        raise ValueError(f"oversold ({p['oversold']}) must be below overbought ({p['overbought']})")


def _rsi_compute(df: pd.DataFrame, p: dict[str, Any]) -> pd.DataFrame:
    r = core.rsi(df["close"], p["period"])
    state = pd.Series(0, index=df.index, dtype="int8")
    state[r < p["oversold"]] = 1
    state[r > p["overbought"]] = -1
    out = events_from_state(state)
    out["rsi"] = r
    return out


_register(
    StrategySpec(
        key="rsi_reversal",
        name="RSI Reversal",
        description="Green while RSI is below the oversold level, red while it is above the overbought level.",
        params=(
            ParamSpec("period", "RSI period", "int", 14, 2, 500, 1),
            ParamSpec("oversold", "Oversold level", "float", 30.0, 0.0, 100.0, 1.0),
            ParamSpec("overbought", "Overbought level", "float", 70.0, 0.0, 100.0, 1.0),
        ),
        compute=_rsi_compute,
        indicators=(IndicatorTemplate("rsi", {"period": "period"}),),
        check=_rsi_check,
    )
)


# ------------------------------------------------------------ EMA crossover
def _ema_check(p: dict[str, Any]) -> None:
    if p["fast"] >= p["slow"]:
        raise ValueError(f"fast ({p['fast']}) must be smaller than slow ({p['slow']})")


def _ema_compute(df: pd.DataFrame, p: dict[str, Any]) -> pd.DataFrame:
    fast = core.ema(df["close"], p["fast"])
    slow = core.ema(df["close"], p["slow"])
    out = events_from_state(sign_state(fast - slow, ref=df["close"]))
    out["ema_fast"] = fast
    out["ema_slow"] = slow
    return out


_register(
    StrategySpec(
        key="ema_cross",
        name="EMA Crossover",
        description="Green while the fast EMA is above the slow EMA, red while it is below. Events fire on the cross.",
        params=(
            ParamSpec("fast", "Fast EMA", "int", 20, 1, 1000, 1),
            ParamSpec("slow", "Slow EMA", "int", 50, 2, 1000, 1),
        ),
        compute=_ema_compute,
        indicators=(IndicatorTemplate("ema", {"period": "fast"}), IndicatorTemplate("ema", {"period": "slow"})),
        check=_ema_check,
    )
)


# --------------------------------------------------------------- MACD cross
def _macd_check(p: dict[str, Any]) -> None:
    if p["fast"] >= p["slow"]:
        raise ValueError(f"fast ({p['fast']}) must be smaller than slow ({p['slow']})")


def _macd_compute(df: pd.DataFrame, p: dict[str, Any]) -> pd.DataFrame:
    m = core.macd(df["close"], p["fast"], p["slow"], p["signal"])
    out = events_from_state(sign_state(m["macd"] - m["signal"], ref=df["close"]))
    out["macd"] = m["macd"]
    out["signal"] = m["signal"]
    return out


_register(
    StrategySpec(
        key="macd_cross",
        name="MACD Cross",
        description="Green while the MACD line is above its signal line, red while below. Events fire on the cross.",
        params=(
            ParamSpec("fast", "Fast period", "int", 12, 1, 500, 1),
            ParamSpec("slow", "Slow period", "int", 26, 2, 1000, 1),
            ParamSpec("signal", "Signal period", "int", 9, 1, 500, 1),
        ),
        compute=_macd_compute,
        indicators=(IndicatorTemplate("macd", {"fast": "fast", "slow": "slow", "signal": "signal"}),),
        check=_macd_check,
    )
)


# --------------------------------------------------------- Bollinger bounce
def _bb_compute(df: pd.DataFrame, p: dict[str, Any]) -> pd.DataFrame:
    b = core.bollinger(df["close"], p["period"], p["mult"])
    close = df["close"].astype(float)
    state = pd.Series(0, index=df.index, dtype="int8")
    state[close < b["lower"]] = 1
    if p["exit_level"] == "middle":
        state[close >= b["middle"]] = -1
    else:
        state[close > b["upper"]] = -1
    out = events_from_state(state)
    for c in ("upper", "middle", "lower"):
        out[c] = b[c]
    return out


_register(
    StrategySpec(
        key="bb_bounce",
        name="Bollinger Bounce",
        description="Green while the close is below the lower band; red while it is above the upper band (or at/above the middle band, if chosen).",
        params=(
            ParamSpec("period", "Period", "int", 20, 2, 1000, 1),
            ParamSpec("mult", "Std dev multiplier", "float", 2.0, 0.1, 10.0, 0.1),
            ParamSpec("exit_level", "Exit level", "choice", "upper", choices=("upper", "middle")),
        ),
        compute=_bb_compute,
        indicators=(IndicatorTemplate("bbands", {"period": "period", "mult": "mult"}),),
    )
)


def get_strategy(key: str) -> StrategySpec:
    try:
        return STRATEGIES[key]
    except KeyError:
        raise ValueError(f"unknown strategy {key!r}; expected one of {sorted(STRATEGIES)}") from None
