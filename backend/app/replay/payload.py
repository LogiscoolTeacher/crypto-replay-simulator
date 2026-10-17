"""Turn a candle DataFrame plus indicator/strategy requests into the JSON payload the UI consumes."""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from ..indicators.registry import get_indicator
from ..strategies.builtin import get_strategy

CANDLE_COLUMNS = ("open", "high", "low", "close", "volume")


def floats(series: pd.Series) -> list[float | None]:
    return [None if math.isnan(v) else float(v) for v in series.to_numpy(dtype=float)]


def build_payload(
    df: pd.DataFrame,
    indicators: list[tuple[str, dict[str, Any]]],
    strategy: tuple[str, dict[str, Any]] | None,
    limit: int,
) -> dict[str, Any]:
    """Compute everything over the whole `df`, return the last `limit` bars, column-oriented.

    Raises ValueError for unknown keys or bad parameters (callers map to HTTP 422).
    Parameters are validated before any computation so bad input fails fast.
    """
    ind_specs = [(get_indicator(key), params) for key, params in indicators]
    strat_spec = (get_strategy(strategy[0]), strategy[1]) if strategy else None

    tail = slice(max(len(df) - limit, 0), len(df))
    out: dict[str, Any] = {
        "count": int(len(df.iloc[tail])),
        "time": df["time"].iloc[tail].astype(int).tolist(),
        **{col: floats(df[col].iloc[tail]) for col in CANDLE_COLUMNS},
        "indicators": [],
        "strategy": None,
    }
    for spec, params in ind_specs:
        full, values = spec.run(df, params)
        out["indicators"].append(
            {"key": spec.key, "params": full, "outputs": {c: floats(values[c].iloc[tail]) for c in values.columns}}
        )
    if strat_spec:
        spec, params = strat_spec
        full, sig = spec.run(df, params)
        extra = [c for c in sig.columns if c not in ("state", "enter", "exit")]
        out["strategy"] = {
            "key": spec.key,
            "params": full,
            "state": sig["state"].iloc[tail].astype(int).tolist(),
            "enter": sig["enter"].iloc[tail].astype(bool).tolist(),
            "exit": sig["exit"].iloc[tail].astype(bool).tolist(),
            "values": {c: floats(sig[c].iloc[tail]) for c in extra},
        }
    return out
