"""Indicator catalogue: parameter schemas, display metadata and computation."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from . import core


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    type: str  # "int" | "float" | "choice"
    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: tuple[str, ...] | None = None

    def coerce(self, value: Any) -> Any:
        if self.type == "int":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError(f"{self.name} must be an integer, got {value!r}")
            try:
                f = float(value)
            except ValueError:
                raise ValueError(f"{self.name} must be an integer, got {value!r}") from None
            if not math.isfinite(f) or f != int(f):
                raise ValueError(f"{self.name} must be an integer, got {value!r}")
            v: Any = int(f)
        elif self.type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError(f"{self.name} must be a number, got {value!r}")
            try:
                v = float(value)
            except ValueError:
                raise ValueError(f"{self.name} must be a number, got {value!r}") from None
            if not math.isfinite(v):
                raise ValueError(f"{self.name} must be a finite number, got {value!r}")
        elif self.type == "choice":
            if value not in (self.choices or ()):
                raise ValueError(f"{self.name} must be one of {list(self.choices or ())}, got {value!r}")
            return value
        else:  # pragma: no cover - programming error
            raise ValueError(f"unknown param type {self.type}")
        if self.min is not None and v < self.min:
            raise ValueError(f"{self.name} must be >= {self.min}, got {v}")
        if self.max is not None and v > self.max:
            raise ValueError(f"{self.name} must be <= {self.max}, got {v}")
        return v


def validate_params(specs: tuple[ParamSpec, ...], given: dict[str, Any] | None) -> dict[str, Any]:
    """Return a full parameter dict: defaults filled in, values coerced, unknown keys rejected."""
    given = dict(given or {})
    known = {p.name for p in specs}
    unknown = set(given) - known
    if unknown:
        raise ValueError(f"unknown parameter(s): {sorted(unknown)}; expected {sorted(known)}")
    out: dict[str, Any] = {}
    for p in specs:
        out[p.name] = p.coerce(given[p.name]) if p.name in given else p.default
    return out


@dataclass(frozen=True)
class OutputSpec:
    name: str
    label: str
    kind: str  # "line" | "histogram"


@dataclass(frozen=True)
class IndicatorSpec:
    key: str
    name: str
    overlay: bool  # True: drawn on the price chart; False: own pane
    params: tuple[ParamSpec, ...]
    outputs: tuple[OutputSpec, ...]
    compute: Callable[[pd.DataFrame, dict[str, Any]], pd.DataFrame]
    levels: tuple[float, ...] = ()  # horizontal reference lines for pane indicators
    range: tuple[float, float] | None = None  # fixed y-range for pane indicators
    check: Callable[[dict[str, Any]], None] | None = None

    def describe(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "overlay": self.overlay,
            "params": [asdict(p) for p in self.params],
            "outputs": [asdict(o) for o in self.outputs],
            "levels": list(self.levels),
            "range": list(self.range) if self.range else None,
        }

    def run(self, df: pd.DataFrame, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], pd.DataFrame]:
        full = validate_params(self.params, params)
        if self.check:
            self.check(full)
        return full, self.compute(df, full)


def _macd_check(p: dict[str, Any]) -> None:
    if p["fast"] >= p["slow"]:
        raise ValueError(f"fast ({p['fast']}) must be smaller than slow ({p['slow']})")


INDICATORS: dict[str, IndicatorSpec] = {}


def _register(spec: IndicatorSpec) -> IndicatorSpec:
    INDICATORS[spec.key] = spec
    return spec


_register(
    IndicatorSpec(
        key="sma",
        name="Simple Moving Average",
        overlay=True,
        params=(ParamSpec("period", "Period", "int", 20, 1, 1000, 1),),
        outputs=(OutputSpec("sma", "SMA", "line"),),
        compute=lambda df, p: pd.DataFrame({"sma": core.sma(df["close"], p["period"])}),
    )
)
_register(
    IndicatorSpec(
        key="ema",
        name="Exponential Moving Average",
        overlay=True,
        params=(ParamSpec("period", "Period", "int", 20, 1, 1000, 1),),
        outputs=(OutputSpec("ema", "EMA", "line"),),
        compute=lambda df, p: pd.DataFrame({"ema": core.ema(df["close"], p["period"])}),
    )
)
_register(
    IndicatorSpec(
        key="rsi",
        name="Relative Strength Index",
        overlay=False,
        params=(ParamSpec("period", "Period", "int", 14, 2, 500, 1),),
        outputs=(OutputSpec("rsi", "RSI", "line"),),
        compute=lambda df, p: pd.DataFrame({"rsi": core.rsi(df["close"], p["period"])}),
        levels=(30.0, 70.0),
        range=(0.0, 100.0),
    )
)
_register(
    IndicatorSpec(
        key="macd",
        name="MACD",
        overlay=False,
        params=(
            ParamSpec("fast", "Fast period", "int", 12, 1, 500, 1),
            ParamSpec("slow", "Slow period", "int", 26, 2, 1000, 1),
            ParamSpec("signal", "Signal period", "int", 9, 1, 500, 1),
        ),
        outputs=(
            OutputSpec("macd", "MACD", "line"),
            OutputSpec("signal", "Signal", "line"),
            OutputSpec("hist", "Histogram", "histogram"),
        ),
        compute=lambda df, p: core.macd(df["close"], p["fast"], p["slow"], p["signal"]),
        levels=(0.0,),
        check=_macd_check,
    )
)
_register(
    IndicatorSpec(
        key="bbands",
        name="Bollinger Bands",
        overlay=True,
        params=(
            ParamSpec("period", "Period", "int", 20, 2, 1000, 1),
            ParamSpec("mult", "Std dev multiplier", "float", 2.0, 0.1, 10.0, 0.1),
        ),
        outputs=(
            OutputSpec("upper", "Upper", "line"),
            OutputSpec("middle", "Middle", "line"),
            OutputSpec("lower", "Lower", "line"),
        ),
        compute=lambda df, p: core.bollinger(df["close"], p["period"], p["mult"]),
    )
)
_register(
    IndicatorSpec(
        key="atr",
        name="Average True Range",
        overlay=False,
        params=(ParamSpec("period", "Period", "int", 14, 1, 500, 1),),
        outputs=(OutputSpec("atr", "ATR", "line"),),
        compute=lambda df, p: pd.DataFrame({"atr": core.atr(df["high"], df["low"], df["close"], p["period"])}),
    )
)

_register(
    IndicatorSpec(
        key="vdelta",
        name="Volume delta",
        overlay=False,
        params=(),
        outputs=(OutputSpec("delta", "Delta", "histogram"),),
        # empty (NaN) where the candles were stored before taker-buy volumes were kept; the view
        # then fills them in the background (CandleStore.fill_taker_buy_soon)
        compute=lambda df, p: pd.DataFrame({"delta": core.volume_delta(
            df["volume"], df["taker_buy"] if "taker_buy" in df else pd.Series(float("nan"), index=df.index))}),
        levels=(0.0,),
    )
)


def get_indicator(key: str) -> IndicatorSpec:
    try:
        return INDICATORS[key]
    except KeyError:
        raise ValueError(f"unknown indicator {key!r}; expected one of {sorted(INDICATORS)}") from None


def compute_indicator(df: pd.DataFrame, key: str, params: dict[str, Any] | None = None) -> tuple[dict, pd.DataFrame]:
    return get_indicator(key).run(df, params)
