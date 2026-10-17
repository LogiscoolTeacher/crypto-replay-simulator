"""Strategy framework.

A strategy turns a candle DataFrame into per-bar signals:

* ``state``  : +1 = entry bias ("green"), -1 = exit bias ("red"), 0 = neutral.
               Warm-up bars (indicators still NaN) are always 0.
* ``enter``  : True on the bar where state becomes +1 (one-bar event).
* ``exit``   : True on the bar where state becomes -1 (one-bar event).

Signals are computed on closed candles only, so the light shown for bar t
uses bar t's close and nothing later.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from ..indicators.registry import ParamSpec, validate_params

SIGNAL_COLUMNS = ("state", "enter", "exit")


def events_from_state(state: pd.Series) -> pd.DataFrame:
    """Derive one-bar enter/exit events from a +1/0/-1 state series."""
    st = state.fillna(0).astype("int8")
    prev = st.shift(1, fill_value=0)
    return pd.DataFrame(
        {
            "state": st,
            "enter": (st == 1) & (prev != 1),
            "exit": (st == -1) & (prev != -1),
        },
        index=state.index,
    )


def sign_state(diff: pd.Series, ref: pd.Series | None = None, rel_tol: float = 1e-9) -> pd.Series:
    """+1 where diff > 0, -1 where diff < 0, 0 where equal or NaN.

    With `ref` (typically the close price), differences smaller than
    rel_tol * |ref| count as zero, so floating-point noise between two lines
    that are mathematically equal (e.g. in a flat market) never produces
    spurious crosses.
    """
    arr = diff.to_numpy(dtype=float)
    if ref is not None:
        tol = rel_tol * np.abs(ref.to_numpy(dtype=float))
        arr = np.where(np.abs(arr) <= tol, 0.0, arr)
    out = np.where(arr > 0, 1, np.where(arr < 0, -1, 0)).astype("int8")
    return pd.Series(out, index=diff.index)


@dataclass(frozen=True)
class IndicatorTemplate:
    """Which indicator instance a strategy displays: indicator param -> strategy param name."""

    key: str
    params: dict[str, str]

    def resolve(self, strategy_params: dict[str, Any]) -> dict[str, Any]:
        return {"key": self.key, "params": {k: strategy_params[v] for k, v in self.params.items()}}


@dataclass(frozen=True)
class StrategySpec:
    key: str
    name: str
    description: str
    params: tuple[ParamSpec, ...]
    compute: Callable[[pd.DataFrame, dict[str, Any]], pd.DataFrame]
    indicators: tuple[IndicatorTemplate, ...]
    check: Callable[[dict[str, Any]], None] | None = None

    def instances(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Indicator instances to draw for these (full) strategy parameters."""
        return [t.resolve(params) for t in self.indicators]

    def describe(self) -> dict:
        defaults = validate_params(self.params, {})
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "params": [asdict(p) for p in self.params],
            "indicators": self.instances(defaults),
            "indicator_templates": [{"key": t.key, "params": dict(t.params)} for t in self.indicators],
        }

    def run(self, df: pd.DataFrame, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], pd.DataFrame]:
        full = validate_params(self.params, params)
        if self.check:
            self.check(full)
        signals = self.compute(df, full)
        missing = [c for c in SIGNAL_COLUMNS if c not in signals.columns]
        if missing:  # pragma: no cover - programming error in a strategy
            raise RuntimeError(f"strategy {self.key} did not return columns {missing}")
        return full, signals
