"""Jump strategies: find the next entry signal on the chart (day-trading mode's "Next signal").

RLine_Retest (1h candles): a qualified resistance zone ("RLine") is broken, then price comes back
to it (the retest). The first time price reaches the broken zone again (or comes within
`near_atr` x ATR of its top) is the entry signal; a confirmation candle is planned for later and
lives in `_retest_signal`.

One forward pass over the candles, strictly causal: what is known at bar t depends only on bars
<= t, so running the pass over the whole replay and reading the signals after the cursor never
leaks the future.

1. Touches. A fractal swing high (the highest high of L bars to the left and R bars to the right)
   becomes known R bars later. It counts as a touch ("moved inside and reversed") once price has
   dropped `rev_atr` x ATR below it without trading above it first.
2. Zones. A touch joins a zone whose band is within `tol_atr` x ATR of its high, else it starts a
   new one. Zones are drawn wick to body: top = highest touch high, bottom = lowest body top
   (max of open and close) of the touch candles, the height clamped to [min_h_atr, max_h_atr] x ATR
   (the largest ATR among the touches), but never so far that a touch high falls outside. A zone without a new
   touch for `lookback_bars` expires; one that is closed above without being an RLine dies.
3. RLine = any of: A) >= 2 touches; B) a touch with volume >= vol_mult x the average of the 20
   bars before it (the swing-high candle or a neighbour); C) a round number (multiples of
   10^floor(log10 price), and of half that) inside the zone or within `psych_atr` x ATR of it.
4. Breakout: a candle closes above the top of an RLine. The zone becomes support-in-waiting.
5. Retest: at least `min_gap` bars after the breakout and after a close of at least top + away_atr x ATR
   (a wick spiking above and snapping back does not count as having moved away), a candle's low comes within `near_atr` x ATR (at the breakout) of the top -> signal. No retest within `retest_max` bars, or a close below
   the zone before it qualifies as a retest -> the zone is dropped. One signal per zone.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..indicators import core
from ..indicators.registry import ParamSpec, validate_params

ATR_PERIOD = 14
VOL_AVG_BARS = 20

RLINE_PARAMS: tuple[ParamSpec, ...] = (
    ParamSpec("pivot_left", "Swing high: bars to the left", "int", 5, 1, 50, 1),
    ParamSpec("pivot_right", "Swing high: bars to the right", "int", 5, 1, 50, 1),
    ParamSpec("rev_atr", "Reversal after a touch (x ATR)", "float", 1.0, 0.0, 20.0, 0.1),
    ParamSpec("tol_atr", "Touch joins a zone within (x ATR)", "float", 0.5, 0.0, 5.0, 0.05),
    ParamSpec("min_h_atr", "Minimum zone height (x ATR)", "float", 0.5, 0.01, 5.0, 0.05),
    ParamSpec("max_h_atr", "Maximum zone height (x ATR)", "float", 1.0, 0.01, 10.0, 0.05),
    ParamSpec("lookback_bars", "Zone expires without a touch after (bars)", "int", 720, 10, 100_000, 1),
    ParamSpec("vol_mult", "High volume (x average of 20 bars)", "float", 2.0, 1.0, 20.0, 0.1),
    ParamSpec("psych_atr", "Round number near the zone within (x ATR)", "float", 0.25, 0.0, 5.0, 0.05),
    ParamSpec("psych_half", "Round numbers", "choice", "full and half", choices=("full and half", "full only")),
    ParamSpec("away_atr", "Move away after the breakout: a close above the top (x ATR)", "float", 0.75, 0.0, 20.0, 0.1),
    ParamSpec("min_gap", "Retest at least (bars after the breakout)", "int", 2, 1, 1000, 1),
    ParamSpec("retest_max", "Retest within (bars after the breakout)", "int", 48, 1, 10_000, 1),
    ParamSpec("near_atr", "Retest counts within (x ATR above the zone)", "float", 0.25, 0.0, 20.0, 0.05),
)


def _rline_check(p: dict[str, Any]) -> None:
    if p["min_h_atr"] > p["max_h_atr"]:
        raise ValueError(f"minimum zone height ({p['min_h_atr']}) must not exceed the maximum ({p['max_h_atr']})")
    if p["min_gap"] > p["retest_max"]:
        raise ValueError(f"min_gap ({p['min_gap']}) must not exceed retest_max ({p['retest_max']})")
    if p["near_atr"] > 0 and p["near_atr"] >= p["away_atr"]:
        raise ValueError(f"near_atr ({p['near_atr']}) must be below away_atr ({p['away_atr']}): coming back has to be closer than moving away")


# ------------------------------------------------------------------ helpers
def round_levels(price: float, half: bool = True) -> tuple[float, float]:
    """The round numbers just below and above `price`: multiples of 10^floor(log10 price)
    (and of half that with `half`). 83 000 -> (80 000, 85 000); 140 -> (100, 150)."""
    step = 10.0 ** math.floor(math.log10(price))
    if half:
        step /= 2.0
    lo = math.floor(price / step) * step
    return lo, lo + step


def near_round(lower: float, upper: float, slack: float, half: bool = True) -> float | None:
    """A round number inside [lower - slack, upper + slack], or None."""
    for p in (lower, upper):
        for lv in round_levels(p, half):
            if lower - slack <= lv <= upper + slack:
                return lv
    # the magnitude may change inside the band (e.g. 99 800 - 100 300): 10^k itself
    top = 10.0 ** math.floor(math.log10(upper))
    return top if lower - slack <= top <= upper + slack else None


@dataclass
class Touch:
    bar: int            # index of the swing-high candle
    known: int          # index of the candle at which it counted as a touch
    high: float
    body_top: float     # max(open, close) of the swing-high candle: the zone is drawn from the wick to the body
    atr: float
    vol_ratio: float    # highest volume of the touch candle and its neighbours / average of the 20 before


@dataclass
class Zone:
    touches: list[Touch] = field(default_factory=list)
    top: float = 0.0
    bottom: float = 0.0
    state: str = "resistance"      # resistance -> broken -> retested
    rules: tuple[str, ...] = ()
    psych: float | None = None
    breakout_bar: int | None = None
    breakout_atr: float = 0.0
    away: bool = False

    def entry_level(self, p: dict[str, Any]) -> float:
        """The price a retest has to reach: the top plus the near-zone buffer."""
        return self.top + p["near_atr"] * self.breakout_atr

    def resize(self, p: dict[str, Any]) -> None:
        atr = max(t.atr for t in self.touches)   # the touch in the most volatile hour sets the scale
        self.top = max(t.high for t in self.touches)
        body = min(t.body_top for t in self.touches)
        height = min(max(self.top - body, p["min_h_atr"] * atr), p["max_h_atr"] * atr)
        # never cut a touch off: every touch high lies inside the zone
        self.bottom = min(self.top - height, min(t.high for t in self.touches))

    def last_touch(self) -> int:
        return self.touches[-1].known

    def qualify(self, atr: float, p: dict[str, Any]) -> tuple[str, ...]:
        rules = []
        if len(self.touches) >= 2:
            rules.append("A")
        if any(t.vol_ratio >= p["vol_mult"] for t in self.touches):
            rules.append("B")
        self.psych = near_round(self.bottom, self.top, p["psych_atr"] * atr, p["psych_half"] == "full and half")
        if self.psych is not None:
            rules.append("C")
        return tuple(rules)


RULE_LABELS = {"A": "2+ touches", "B": "high volume", "C": "round number"}


def _retest_signal(z: Zone, i: int, low: np.ndarray, p: dict[str, Any]) -> bool:
    """The entry rule on a broken zone at bar i: price comes back to the zone, i.e. within the
    near-zone buffer above its top. (A confirmation candle, e.g. a bullish close back above the
    top, is planned and belongs here.)"""
    return bool(low[i] <= z.entry_level(p))


# ---------------------------------------------------------------- detector
def detect_rline(df: pd.DataFrame, params: dict[str, Any] | None = None) -> dict:
    """Zones and retest signals of RLine_Retest on `df` (columns time [unix s], open, high, low,
    close, volume; ordered, one timeframe). Returns {"params", "signals", "zones"}: a signal is
    {bar, bar_ms, zone_top, zone_bottom, entry_level, near, rules, touches, touch_ms, breakout_ms,
    psych} (`near`: the low stayed above the top, inside the buffer); zones are
    those still alive at the last bar (resistance or broken)."""
    p = validate_params(RLINE_PARAMS, params)
    _rline_check(p)
    n = len(df)
    if n == 0:
        return {"params": p, "signals": [], "zones": []}
    t_ms = df["time"].to_numpy(dtype="int64") * 1000
    opn = df["open"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    atr = core.atr(df["high"], df["low"], df["close"], ATR_PERIOD).to_numpy(dtype=float) if n > ATR_PERIOD else np.full(n, np.nan)
    vol_avg = pd.Series(vol).rolling(VOL_AVG_BARS, min_periods=VOL_AVG_BARS).mean().shift(1).to_numpy()
    L, R = p["pivot_left"], p["pivot_right"]

    pending: list[dict] = []     # swing highs waiting for their reversal
    zones: list[Zone] = []
    signals: list[dict] = []

    for i in range(n):
        # 1. zones against the closed candle i (touches known before i only)
        alive: list[Zone] = []
        for z in zones:
            if z.state == "resistance":
                if close[i] > z.top:
                    rules = z.qualify(atr[i] if math.isfinite(atr[i]) else z.touches[-1].atr, p)
                    if not rules:
                        continue                 # closed above a plain zone: it is gone
                    z.state, z.rules, z.breakout_bar = "broken", rules, i
                    z.breakout_atr = atr[i] if math.isfinite(atr[i]) else z.touches[-1].atr
                    z.away = close[i] >= z.top + p["away_atr"] * z.breakout_atr
                elif i - z.last_touch() > p["lookback_bars"]:
                    continue
                alive.append(z)
                continue
            # broken: waiting for the retest
            age = i - z.breakout_bar  # type: ignore[operator]
            if age > p["retest_max"]:
                continue
            if z.away and age >= p["min_gap"] and _retest_signal(z, i, low, p):
                z.state = "retested"
                signals.append({
                    "bar": i, "bar_ms": int(t_ms[i]), "zone_top": float(z.top), "zone_bottom": float(z.bottom),
                    "entry_level": float(z.entry_level(p)), "near": bool(low[i] > z.top),
                    "rules": list(z.rules), "touches": len(z.touches), "touch_ms": [int(t_ms[t.bar]) for t in z.touches],
                    "breakout_ms": int(t_ms[z.breakout_bar]), "psych": z.psych,  # type: ignore[index]
                })
                continue
            if close[i] < z.bottom:
                continue                         # back below before a valid retest: failed breakout
            if close[i] >= z.top + p["away_atr"] * z.breakout_atr:
                z.away = True
            alive.append(z)
        zones = alive

        # 2. the swing high at i - R is now known
        j = i - R
        if j >= L and math.isfinite(atr[j]):
            hj = high[j]
            if hj >= high[j - L:j].max() and (R == 0 or hj > high[j + 1:i + 1].max()):
                va = vol_avg[j - 1]              # the 20 bars before the touch candles
                vmax = vol[j - 1:j + 2].max()    # the swing-high candle or a neighbour (equal highs at the turn)
                ratio = float(vmax / va) if math.isfinite(va) and va > 0 else 0.0
                pending.append({"bar": j, "high": float(hj), "body": float(max(opn[j], close[j])), "atr": float(atr[j]), "ratio": ratio, "low": float(low[j + 1:i + 1].min())})
        # 3. pending swing highs: reversed far enough -> touch; traded above -> dropped
        still: list[dict] = []
        for pv in pending:
            if pv["bar"] + R < i:                # bars after the one that made it known
                if high[i] > pv["high"]:
                    continue
                pv["low"] = min(pv["low"], low[i])
            if pv["high"] - pv["low"] >= p["rev_atr"] * pv["atr"]:
                _add_touch(zones, Touch(pv["bar"], i, pv["high"], pv["body"], pv["atr"], pv["ratio"]), p)
            elif i - pv["bar"] <= p["lookback_bars"]:
                still.append(pv)
        pending = still

    return {
        "params": p,
        "signals": signals,
        "zones": [
            {"top": float(z.top), "bottom": float(z.bottom), "state": z.state, "touches": len(z.touches), "rules": list(z.rules),
             "touch_ms": [int(t_ms[t.bar]) for t in z.touches],
             "breakout_ms": int(t_ms[z.breakout_bar]) if z.breakout_bar is not None else None}
            for z in zones
        ],
    }


def _add_touch(zones: list[Zone], t: Touch, p: dict[str, Any]) -> None:
    tol = p["tol_atr"] * t.atr
    best, dist = None, math.inf
    for z in zones:
        if z.state != "resistance":
            continue
        d = 0.0 if z.bottom <= t.high <= z.top else min(abs(t.high - z.top), abs(t.high - z.bottom))
        if d <= tol and d < dist:
            best, dist = z, d
    if best is None:
        best = Zone()
        zones.append(best)
    best.touches.append(t)
    best.resize(p)


def describe_signal(sig: dict) -> str:
    rules = ", ".join(RULE_LABELS[r] for r in sig["rules"])
    near = f", came within {sig['entry_level'] - sig['zone_top']:.6g} of it" if sig.get("near") else ""
    return f"retest of {sig['zone_bottom']:.6g} – {sig['zone_top']:.6g}{near} ({sig['touches']} touch{'es' if sig['touches'] != 1 else ''}; {rules})"


# ------------------------------------------------------------------ registry
@dataclass(frozen=True)
class JumpStrategy:
    key: str
    name: str
    description: str
    interval: str
    params: tuple[ParamSpec, ...]
    detect: Callable[[pd.DataFrame, dict[str, Any] | None], dict]
    describe_signal: Callable[[dict], str]

    def describe(self) -> dict:
        return {"key": self.key, "name": self.name, "description": self.description, "interval": self.interval,
                "params": [asdict(x) for x in self.params]}


JUMP_STRATEGIES: dict[str, JumpStrategy] = {
    "rline_retest": JumpStrategy(
        key="rline_retest",
        name="RLine_Retest",
        description="1h: a resistance zone with 2+ touches, a high-volume touch or a round number is broken; "
                    "after a close 0.75 ATR above it, the signal is the first time price comes back into it.",
        interval="1h",
        params=RLINE_PARAMS,
        detect=detect_rline,
        describe_signal=describe_signal,
    ),
}


def get_jump_strategy(key: str) -> JumpStrategy:
    try:
        return JUMP_STRATEGIES[key]
    except KeyError:
        raise ValueError(f"unknown strategy {key!r}; expected one of {sorted(JUMP_STRATEGIES)}") from None
