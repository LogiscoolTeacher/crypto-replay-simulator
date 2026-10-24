"""Replay engine: sessions, the cursor, the account and its buckets.

Time model
----------
* A session replays one symbol at a *base* interval (its finest timeframe).
  The cursor is the open time of the latest base candle the user may see.
  Everything with open_time <= cursor at the base interval is "known".
* Coarser *view* intervals show every candle that has fully closed by the
  cursor's close time, plus one partial candle aggregated from base candles.
  A view interval finer than the base is not allowed.
* Stepping at the base interval moves to the next stored base candle.
  Stepping at a coarser interval moves to the base candle that completes the
  current (or next) candle of that interval.

Trading model
-------------
* Fills happen at the close of the cursor's base candle (the latest known price).
* Buy: spend `amount` quote; fee = amount * fee_rate; qty = (amount - fee) / price.
* Sell closes a whole bucket: proceeds = qty * price; fee = proceeds * fee_rate;
  cash += proceeds - fee; realized P&L = proceeds - fee - cost.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import math
import shutil
import threading
from pathlib import Path
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from .. import config
from ..data.candle_store import CandleStore, floor_to_slot, last_closed_open_time
from ..indicators import core
from .payload import build_payload
from .performance import combined_report, json_safe, session_report
from .reasons import CHECKLIST_VERSION, clean as clean_reasons, stats as reason_stats_over
from .session_store import SessionStore

log = logging.getLogger(__name__)

WARMUP_BARS = 1000  # bars of history downloaded before the session start, per view interval
# Indicators are computed over a sliding window of the most recent bars ending at the cursor.
# EMA/RSI recursions have forgotten their seed after a few thousand bars (an EMA(200) gives a
# bar 3000 back a weight of e^-30), so values equal a full-history computation to within
# floating-point noise, while the per-step cost stays constant on long 1-minute sessions.
COMPUTE_WARMUP_BARS = 3000
# Volume delta of candles stored before taker-buy volumes were kept: the view fills them in the
# background (CandleStore.fill_taker_buy_soon). Filling only up to the cursor left the newest bar
# empty after every step (the step's view answers before its fill lands), so the fill runs
# TAKER_FILL_AHEAD bars past the cursor (one exchange request) as soon as one of the next
# TAKER_CHECK_AHEAD bars lacks the volume: every few hundred steps one request, well in time.
TAKER_CHECK_AHEAD = 200
TAKER_FILL_AHEAD = 1000
MIN_ORDER_QUOTE = 1.0
RSI_PERIOD = 14          # RSI recorded with every buy and sell
RSI_WARMUP_BARS = 600    # bars fed to that RSI; far beyond where the seed stops mattering
RSI_PANES_MAX = 4        # extra RSI panes (other timeframes) a view may ask for
EMA_LONG_PERIOD = 200    # side-panel gauge: EMA 200 on daily candles
EMA_LONG_INTERVAL = "1D"
EMA_LONG_WARMUP_BARS = 1200
# A new session starts no earlier than this many daily candles after the coin's first Binance candle,
# so the daily EMA/SMA 200 (and the chart averages on every timeframe) exist from the first bar.
TP_MAX_PCT = 1000.0   # a target may be at most ten times the entry price above it
SL_MAX_PCT = 100.0    # a stop must be above zero
MIN_WARMUP_DAYS = 200
EXTREMES_INTERVAL = "1D"  # side-panel gauge and journal: distance to the highs and lows of daily candles
EXTREME_WINDOWS = (("all", None), ("1y", 365), ("6m", 182), ("3m", 90))  # key -> days before the replay day (None: since listing)
EXTREME_JOURNAL_KEYS = {"all": ("ath", "atl"), "1y": ("high_1y", "low_1y"), "6m": ("high_6m", "low_6m"), "3m": ("high_3m", "low_3m")}
HISTORY_START_MS = int(datetime(2017, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)  # before Binance listed anything
HISTORY_RETRY_SECONDS = 60.0        # after a failed history download, wait this long before trying again
BB_PERIOD = 20           # side-panel gauge: Bollinger Bands 20 / 2 on the timeframe on screen
BB_MULT = 2.0
FIB_LEVELS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
AUTO_FIB_MAX_LOOKBACK = 2000
FVG_MAX_MIN_PCT = 10.0
CONTEXT_SYMBOL = "BTCUSDT"        # the market-context peek always shows Bitcoin
CONTEXT_INTERVAL = "1D"
CONTEXT_YEARS_MAX = 5
CONTEXT_WARMUP_DAYS = 300          # extra days fetched so overlay indicators are settled at the window start
SR_PIVOT_BARS = 5                  # a swing high/low must top its 5 neighbours on each side
SR_TOL_PCT = 1.5                   # swings within this percent of each other form one level
SR_TOL_MAX_PCT = 10.0
SR_LEVELS = 3                      # levels drawn on each side of the price
NEAR_LEVEL_DAYS = 365              # readings: next support/resistance from the closed daily candles of the last year
NEAR_LEVEL_MIN_TOUCHES = 3         # a zone must have been touched this often to count
ZONE_MAX_VISIT_BARS = 5            # a visit of a zone counts as a touch when the price leaves within this many daily candles
SNAPSHOT_MAX_BYTES = 8_000_000       # one chart picture
SNAPSHOT_EXTRA_MAX_CHARS = 20_000    # UI readings merged into a journal row
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SNAPSHOT_DATA_URL_PREFIX = "data:image/png;base64,"  # smallest buy, in quote currency
EARLIEST_START_MS = int(datetime(2010, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
MAX_BALANCE = 1e12
SESSION_MODES = ("swing", "day")  # buckets (this module) or day-trading (day_engine.py)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ReplayError(Exception):
    """Base class; subclasses map to HTTP status codes in the API layer."""


class NotFound(ReplayError):
    pass


class Invalid(ReplayError):
    pass


class Conflict(ReplayError):
    pass


def decode_png_data_url(image: str) -> bytes:
    """The PNG bytes of a `data:image/png;base64,...` chart picture; Invalid when it is not one."""
    if not isinstance(image, str) or not image.startswith(SNAPSHOT_DATA_URL_PREFIX):
        raise Invalid("image must be a PNG data URL")
    try:
        data = base64.b64decode(image[len(SNAPSHOT_DATA_URL_PREFIX):], validate=True)
    except (ValueError, TypeError):
        raise Invalid("image is not valid base64") from None
    if not data.startswith(PNG_SIGNATURE):
        raise Invalid("image is not a PNG")
    if len(data) > SNAPSHOT_MAX_BYTES:
        raise Invalid(f"image exceeds {SNAPSHOT_MAX_BYTES} bytes")
    return data


def _iso(ms: int | None) -> str | None:
    # Arithmetic rather than fromtimestamp: Windows raises OSError for pre-1970 values.
    return None if ms is None else (_EPOCH + timedelta(milliseconds=int(ms))).isoformat(timespec="minutes")


def qtyf(q: float) -> str:
    return f"{q:g}" if q == int(q) else f"{q:.6f}".rstrip("0")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def swing_levels(df: pd.DataFrame, tol_pct: float = SR_TOL_PCT, pivot_bars: int = SR_PIVOT_BARS) -> tuple[list[dict], int]:
    """Horizontal levels from the swing highs and lows of `df` (time in seconds, OHLC), and the swing count.

    A swing high is a bar whose high beats the `pivot_bars` bars before it and is not beaten by the
    `pivot_bars` bars after it (swing lows mirror this). Swings are sorted by price and grouped
    while each stays within `tol_pct` percent of the group's mean; every group is one level at
    that mean with one touch per swing. A level scores one point per touch plus up to half a point
    more for a recent touch, and one extra point when it holds the window's highest high or lowest
    low. `touch_ms` lists the touch times, oldest first.
    """
    if not (0 < tol_pct <= SR_TOL_MAX_PCT) or not math.isfinite(tol_pct):
        raise Invalid(f"tol_pct must be between 0 and {SR_TOL_MAX_PCT}")
    n = len(df)
    if n < 2 * pivot_bars + 1:
        return [], 0
    highs, lows, times = df["high"].to_numpy(float), df["low"].to_numpy(float), df["time"].to_numpy(int)
    hi_all, lo_all = float(highs.max()), float(lows.min())
    swings: list[tuple[float, int]] = []  # (price, bar index)
    k = pivot_bars
    for i in range(k, n - k):
        h, lo = highs[i], lows[i]
        if h > highs[i - k:i].max() and h >= highs[i + 1:i + k + 1].max():
            swings.append((float(h), i))
        if lo < lows[i - k:i].min() and lo <= lows[i + 1:i + k + 1].min():
            swings.append((float(lo), i))
    if not swings:
        return [], 0
    swings.sort()
    groups: list[list[tuple[float, int]]] = [[swings[0]]]
    for p, i in swings[1:]:
        mean = sum(q for q, _ in groups[-1]) / len(groups[-1])
        if p <= mean * (1 + tol_pct / 100):
            groups[-1].append((p, i))
        else:
            groups.append([(p, i)])
    levels = []
    for g in groups:
        prices = [p for p, _ in g]
        idx = [i for _, i in g]
        level = sum(prices) / len(prices)
        score = sum(1 + 0.5 * i / max(n - 1, 1) for i in idx)
        if any(p == hi_all for p in prices) or any(p == lo_all for p in prices):
            score += 1
        touch_ms = sorted(int(times[i]) * 1000 for i in idx)
        levels.append({
            "price": level, "touches": len(g), "score": round(score, 3),
            "first_ms": touch_ms[0], "last_ms": touch_ms[-1],
            "first_iso": _iso(touch_ms[0]), "last_iso": _iso(touch_ms[-1]),
            "touch_ms": touch_ms,
        })
    return levels, len(swings)


def support_resistance(
    df: pd.DataFrame, price: float, tol_pct: float = SR_TOL_PCT, pivot_bars: int = SR_PIVOT_BARS, n_levels: int = SR_LEVELS
) -> dict:
    """Major horizontal levels of `df` (see `swing_levels`): the `n_levels` best-scoring levels
    above `price` are the resistances, the best below it the supports, each list nearest-first
    and labelled R1.. / S1.. with the distance from `price` in percent."""
    levels, swings = swing_levels(df, tol_pct, pivot_bars)
    if not levels or not math.isfinite(price):
        return {"resistance": [], "support": [], "swings": 0}
    above = sorted((l for l in levels if l["price"] > price), key=lambda l: -l["score"])[:n_levels]
    below = sorted((l for l in levels if l["price"] <= price), key=lambda l: -l["score"])[:n_levels]
    above.sort(key=lambda l: l["price"])
    below.sort(key=lambda l: -l["price"])
    for tag, lst in (("R", above), ("S", below)):
        for j, l in enumerate(lst, 1):
            l["label"] = f"{tag}{j}"
            l["pct"] = (l["price"] / price - 1) * 100 if price else None
    return {"resistance": above, "support": below, "swings": swings}


def zone_visits(df: pd.DataFrame, low: float, high: float, max_bars: int = ZONE_MAX_VISIT_BARS) -> list[dict]:
    """Every finished visit of the price to the band [low, high] in `df` (time in seconds, OHLC).

    A visit starts with a candle whose range reaches into the band while the candle before closed
    outside it (that side is where the price came from) and ends with the first candle that stays
    out of the band again. It is a `touch` when the price leaves on the side it came from within
    `max_bars` candles, a `break` when it leaves on the other side within that time, and a `stay`
    when it lingered longer. A visit still open at the last candle, or one starting on the first
    candle (origin unknown), is not counted."""
    highs, lows, closes = (df[c].to_numpy(float) for c in ("high", "low", "close"))
    times = df["time"].to_numpy(int)
    out: list[dict] = []
    i, n = 1, len(df)
    while i < n:
        if not (highs[i] >= low and lows[i] <= high):
            i += 1
            continue
        prev = closes[i - 1]
        origin = "below" if prev < low else "above" if prev > high else None
        j = i + 1
        while j < n and highs[j] >= low and lows[j] <= high:
            j += 1
        if j >= n or origin is None:
            i = j
            continue
        exit_side = "above" if closes[j] > high else "below"
        bars = j - i
        result = "stay" if bars > max_bars else "touch" if exit_side == origin else "break"
        out.append({"ms": int(times[i]) * 1000, "date": _iso(int(times[i]) * 1000)[:10], "from": origin,
                    "bars": int(bars), "result": result})
        i = j
    return out


def nearest_levels(
    df: pd.DataFrame, price: float, tol_pct: float = SR_TOL_PCT, pivot_bars: int = SR_PIVOT_BARS,
    min_touches: int = NEAR_LEVEL_MIN_TOUCHES, max_visit_bars: int = ZONE_MAX_VISIT_BARS,
) -> dict:
    """The next resistance and support as zones ("tubes"): each swing level of `df` (see
    `swing_levels`) becomes a band of +-`tol_pct` percent around its centre, and its visits are
    counted with `zone_visits`. Zones with at least `min_touches` touches qualify; the nearest
    one whose centre lies above `price` is the resistance, the nearest at or below it the support.
    `pct` is the distance from `price` to the zone's nearer edge (0 with `inside` when the price
    is in the band); `touch_dates` are the entry days of the touches, `visits` every visit."""
    levels, swings = swing_levels(df, tol_pct, pivot_bars)
    out = {"resistance": None, "support": None, "levels": 0, "swings": swings,
           "min_touches": min_touches, "tol_pct": tol_pct, "max_visit_bars": max_visit_bars}
    if not levels or not (math.isfinite(price) and price > 0):
        return out
    zones = []
    for l in levels:
        lo, hi = l["price"] * (1 - tol_pct / 100), l["price"] * (1 + tol_pct / 100)
        visits = zone_visits(df, lo, hi, max_visit_bars)
        touches = [v for v in visits if v["result"] == "touch"]
        if len(touches) < min_touches:
            continue
        zones.append({
            "price": l["price"], "low": lo, "high": hi, "swings": l["touches"], "score": l["score"],
            "touches": len(touches), "breaks": sum(v["result"] == "break" for v in visits),
            "stays": sum(v["result"] == "stay" for v in visits),
            "first_ms": touches[0]["ms"], "last_ms": touches[-1]["ms"],
            "first_iso": _iso(touches[0]["ms"]), "last_iso": _iso(touches[-1]["ms"]),
            "touch_ms": [v["ms"] for v in touches], "touch_dates": [v["date"] for v in touches], "visits": visits,
        })
    out["levels"] = len(zones)
    if not zones:
        return out
    above = [z for z in zones if z["price"] > price]
    below = [z for z in zones if z["price"] <= price]

    def pub(z: dict, edge: str) -> dict:
        inside = z["low"] <= price <= z["high"]
        return {**z, "inside": inside, "pct": 0.0 if inside else (z[edge] / price - 1) * 100}

    if above:
        out["resistance"] = pub(min(above, key=lambda z: z["price"]), "low")
    if below:
        out["support"] = pub(max(below, key=lambda z: z["price"]), "high")
    return out


def fair_value_gaps(df: pd.DataFrame, min_pct: float) -> list[dict]:
    """Three-candle imbalances in `df` (time in seconds, OHLC columns), oldest first.

    Bullish: low of candle 3 above high of candle 1; bearish: high of candle 3 below low of candle 1.
    The box spans the gap, starts at the middle candle and is filled once a later candle trades
    through the whole gap (bullish: a low at or below the gap bottom; bearish: a high at or above
    the top). Gaps smaller than `min_pct` percent of the middle candle's close are ignored.
    """
    if not (0 <= min_pct <= FVG_MAX_MIN_PCT) or not math.isfinite(min_pct):
        raise Invalid(f"fvg min_pct must be between 0 and {FVG_MAX_MIN_PCT}")
    n = len(df)
    if n < 3:
        return []
    t = df["time"].to_numpy()
    hi = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    cl = df["close"].to_numpy(dtype=float)
    out: list[dict] = []
    for i in range(2, n):
        if lo[i] > hi[i - 2]:
            dir_, top, bottom = "bull", float(lo[i]), float(hi[i - 2])
        elif hi[i] < lo[i - 2]:
            dir_, top, bottom = "bear", float(lo[i - 2]), float(hi[i])
        else:
            continue
        ref = cl[i - 1]
        size_pct = (top - bottom) / ref * 100.0 if ref > 0 else 0.0
        if size_pct < min_pct:
            continue
        t_end = None
        for j in range(i + 1, n):
            if (dir_ == "bull" and lo[j] <= bottom) or (dir_ == "bear" and hi[j] >= top):
                t_end = int(t[j]) * 1000
                break
        out.append({
            "dir": dir_, "top": top, "bottom": bottom, "size_pct": size_pct,
            "t_start_ms": int(t[i - 1]) * 1000, "t_end_ms": t_end, "filled": t_end is not None,
        })
    return out


def fib_levels(p1: float, p2: float) -> list[dict]:
    """Retracement levels between the anchors: 0 % at p2 (the swing end), 100 % at p1."""
    return [{"ratio": r, "price": p2 + (p1 - p2) * r} for r in FIB_LEVELS]


def aggregate(base: pd.DataFrame, open_time_s: int) -> dict[str, float]:
    """Aggregate base candles (DataFrame with open/high/low/close/volume/taker_buy) into one candle."""
    return {
        "time": int(open_time_s),
        "open": float(base["open"].iloc[0]),
        "high": float(base["high"].max()),
        "low": float(base["low"].min()),
        "close": float(base["close"].iloc[-1]),
        "volume": float(base["volume"].sum()),
        # NaN as soon as one base candle lacks it (stored before taker-buy volumes were kept)
        "taker_buy": float(base["taker_buy"].sum(min_count=len(base))) if "taker_buy" in base else float("nan"),
    }


class ReplayEngine:
    def __init__(self, candles: CandleStore, sessions: SessionStore) -> None:
        self.candles = candles
        self.sessions = sessions
        self._lock = threading.RLock()
        # Daily history since the listing, per symbol: open time of the last closed day it is known
        # to be complete up to, and the clock time before which no retry is attempted after a failure.
        self._history_until: dict[str, int] = {}
        self._history_retry: dict[str, float] = {}

    # ---------------------------------------------------------------- sessions
    def create_session(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int | None = None,
        base_interval: str = "1h",
        start_balance: float = config.DEFAULT_START_BALANCE,
        fee_rate: float = config.DEFAULT_FEE_RATE,
        name: str | None = None,
        mode: str = "swing",
    ) -> dict:
        """`mode` is "swing" (buckets, everything in this class) or "day" (day-trading mode: the
        session row and the data are shared, trading lives in day_engine.py)."""
        try:
            symbol = config.validate_symbol(symbol)
            base_ms = config.interval_ms(base_interval)
        except ValueError as exc:
            raise Invalid(str(exc)) from None
        if mode not in SESSION_MODES:
            raise Invalid(f"mode must be one of {list(SESSION_MODES)}")
        if not (math.isfinite(start_balance) and 0 < start_balance <= MAX_BALANCE):
            raise Invalid(f"start_balance must be a positive number up to {MAX_BALANCE:.0f}")
        if not (math.isfinite(fee_rate) and 0 <= fee_rate < 0.1):
            raise Invalid("fee_rate must be between 0 and 0.1 (10%)")
        now_ms = int(time.time() * 1000)
        latest = last_closed_open_time(now_ms, base_ms)
        if end_ms is None:
            end_ms = latest
        end_ms = min(int(end_ms), latest)
        start_ms = int(start_ms)
        if start_ms < EARLIEST_START_MS:
            raise Invalid("start must be 2010-01-01 or later")
        if end_ms <= start_ms:
            raise Invalid("end must be after start (and start must lie in the past)")
        sid = uuid.uuid4().hex[:12]
        name = name or self._default_name(symbol, base_interval, start_ms)
        now = _now_iso()
        with self.sessions.tx() as conn:
            return self._public_session(
                self.sessions.create_session(
                    conn,
                    id=sid, name=name, symbol=symbol, base_interval=base_interval,
                    start_ms=start_ms, end_ms=end_ms, data_start_ms=None, data_end_ms=None, cursor_ms=None,
                    start_balance=float(start_balance), cash=float(start_balance), fee_rate=float(fee_rate),
                    status="preparing", error=None, start_note=None, created_at=now, updated_at=now, mode=mode,
                )
            )

    def prepare(self, session_id: str, progress=None) -> dict:
        """Download the candles the session needs, then mark it ready. Safe to re-run."""
        s = self._get(session_id)
        base_ms = config.interval_ms(s["base_interval"])
        with self.sessions.tx() as conn:
            self.sessions.update_session(conn, session_id, status="preparing", error=None, updated_at=_now_iso())
        try:
            for iv, iv_ms in config.INTERVALS_MS.items():
                if iv_ms < base_ms:
                    continue
                anchor = s["start_ms"] - WARMUP_BARS * iv_ms
                self.candles.ensure_range(s["symbol"], iv, anchor, s["end_ms"], progress=progress)
            first = self.candles.first_at_or_after(s["symbol"], s["base_interval"], s["start_ms"])
            last = self.candles.last_at_or_before(s["symbol"], s["base_interval"], s["end_ms"])
            if first is None or last is None or first > last:
                raise Invalid(f"no {s['base_interval']} candles for {s['symbol']} between {_iso(s['start_ms'])} and {_iso(s['end_ms'])}")
            first, note, moved = self._place_start(s, first, last)
        except Exception as exc:
            log.exception("preparing session %s failed", session_id)
            with self.sessions.tx() as conn:
                self.sessions.update_session(conn, session_id, status="error", error=str(exc), updated_at=_now_iso())
            raise
        with self._lock, self.sessions.tx() as conn:
            fields: dict[str, Any] = {"status": "ready", "error": None, "data_start_ms": first, "data_end_ms": last, "start_note": note}
            if moved and s["name"] == self._default_name(s["symbol"], s["base_interval"], s["start_ms"]):
                fields["name"] = self._default_name(s["symbol"], s["base_interval"], first)  # an own name stays
            cur = self.sessions.get_session(conn, session_id)["cursor_ms"]  # type: ignore[index]
            if cur is None or cur < first or cur > last:
                fields["cursor_ms"] = first
            self.sessions.update_session(conn, session_id, updated_at=_now_iso(), **fields)
        return self.state(session_id)

    @staticmethod
    def _default_name(symbol: str, base_interval: str, start_ms: int) -> str:
        return f"{symbol} {base_interval} from {_iso(start_ms)[:10]}"

    def _place_start(self, s: dict, first: int, last: int) -> tuple[int, str | None, bool]:
        """Where the replay begins: `first` (the first base candle at or after the chosen start),
        unless fewer than MIN_WARMUP_DAYS daily candles lie before it. Then the start moves to the
        first base candle on or after the 200th day after the coin's first Binance candle, so the
        moving averages exist from the first bar. A coin too young for that starts where it was
        asked to and gets a warning instead. Returns (first, note for the user, moved)."""
        day_ms = config.interval_ms(EMA_LONG_INTERVAL)
        # The daily download reaches WARMUP_BARS days before the chosen start, so this is the coin's
        # listing day when the start lies within those days; otherwise enough candles exist anyway.
        listing = self.candles.first_at_or_after(s["symbol"], EMA_LONG_INTERVAL, s["start_ms"] - WARMUP_BARS * day_ms)
        if listing is None:
            return first, None, False
        earliest = listing + MIN_WARMUP_DAYS * day_ms
        if first >= earliest:
            return first, None, False
        sym, date = s["symbol"], lambda ms: _iso(ms)[:10]
        candidate = self.candles.first_at_or_after(sym, s["base_interval"], earliest)
        if candidate is not None and candidate <= last:
            note = (f"Start moved from {date(s['start_ms'])} to {date(candidate)}: the first {sym} candle on Binance is from "
                    f"{date(listing)}, and {MIN_WARMUP_DAYS} daily candles before the start are needed for the moving averages.")
            return candidate, note, True
        days = (floor_to_slot(first, day_ms) - listing) // day_ms
        count = "no daily candle" if days == 0 else f"only {days} daily candle{'' if days == 1 else 's'}"
        note = (f"{sym} has {count} before the start (first candle on Binance: {date(listing)}). "
                f"The {MIN_WARMUP_DAYS}-day moving averages appear from {date(earliest)}.")
        return first, note, False

    def list_sessions(self) -> list[dict]:
        with self.sessions.tx() as conn:
            return [self._public_session(s) for s in self.sessions.list_sessions(conn)]

    def delete_session(self, session_id: str) -> None:
        with self._lock, self.sessions.tx() as conn:
            if not self.sessions.delete_session(conn, session_id):
                raise NotFound(f"session {session_id} not found")
        self._remove_snapshot_files(session_id)

    # ------------------------------------------------------------------- state
    def state(self, session_id: str) -> dict:
        with self.sessions.tx() as conn:
            s = self.sessions.get_session(conn, session_id)
            if s is None:
                raise NotFound(f"session {session_id} not found")
            buckets = self.sessions.list_buckets(conn, session_id)
        out = self._public_session(s)
        price = self._price(s) if s["status"] == "ready" else None
        out["price"] = price
        out["finished"] = bool(s["status"] == "ready" and s["cursor_ms"] >= s["data_end_ms"])
        open_b = [
            self._public_bucket(b, price, self._excursion(s, b["opened_ms"], s["cursor_ms"]) if price is not None else None, buckets)
            for b in buckets if b["status"] == "open"
        ]
        closed = [b for b in buckets if b["status"] == "closed"]
        invested = sum(b["cost"] for b in open_b)
        market_value = sum(b["market_value"] for b in open_b) if price is not None else 0.0
        realized = sum(b["realized_pnl"] for b in closed)
        fees = sum(b["buy_fee"] for b in buckets) + sum(b["sell_fee"] for b in closed)
        equity = s["cash"] + market_value
        out["account"] = {
            "cash": s["cash"],
            "invested": invested,
            "market_value": market_value,
            "unrealized_pnl": market_value - invested,
            "realized_pnl": realized,
            "fees_paid": fees,
            "equity": equity,
            "total_return": equity - s["start_balance"],
            "total_return_pct": (equity / s["start_balance"] - 1.0) * 100.0,
            "open_buckets": len(open_b),
            "closed_buckets": len(closed),
        }
        out["buckets"] = open_b
        out["reference"] = self._reference(s, buckets, closed, price)
        return out

    def _reference(self, s: dict, buckets: list[dict], closed: list[dict], price: float | None) -> dict:
        """Prices to judge a buy right now against: the latest buy and the latest sell, if any.

        Once the market has closed above the trade price since then, the comparison basis becomes
        that running peak (highest base-candle close), so the percentage reads as the distance
        below the last height rather than above the old trade.
        """
        last_buy = max(buckets, key=lambda b: (b["opened_ms"], b["seq"])) if buckets else None
        last_sell = max(closed, key=lambda b: (b["closed_ms"], b["seq"])) if closed else None

        def point(b: dict | None, key: str, ms_key: str) -> dict | None:
            if b is None:
                return None
            ref = b[key]
            peak = None
            if price is not None:
                peak = self.candles.peak_close(s["symbol"], s["base_interval"], b[ms_key], s["cursor_ms"])
            use_peak = peak is not None and peak[1] > ref
            basis = peak[1] if use_peak else ref
            pct = (price / basis - 1.0) * 100.0 if price is not None and basis else None
            return {
                "seq": b["seq"], "part": b.get("part"), "price": ref, "ms": b[ms_key], "iso": _iso(b[ms_key]), "pct": pct,
                "basis": "peak" if use_peak else "price",
                "peak": peak[1] if use_peak else None,
                "peak_ms": peak[0] if use_peak else None,
                "peak_iso": _iso(peak[0]) if use_peak else None,
            }

        return {
            "last_buy": point(last_buy, "entry_price", "opened_ms"),
            "last_sell": point(last_sell, "exit_price", "closed_ms"),
            "last_closed": self._public_bucket(last_sell, price) if last_sell is not None else None,
        }

    def trades(self, session_id: str) -> dict:
        with self.sessions.tx() as conn:
            s = self.sessions.get_session(conn, session_id)
            if s is None:
                raise NotFound(f"session {session_id} not found")
            trades = self.sessions.list_trades(conn, session_id)
            buckets = self.sessions.list_buckets(conn, session_id)
            # Buckets closed before the held high/low columns existed get them now.
            for b in buckets:
                if b["status"] == "closed" and b.get("held_high") is None:
                    ex = self._excursion(s, b["opened_ms"], b["closed_ms"])
                    if ex:
                        fields = {"held_high": ex["high"], "held_high_ms": ex["high_ms"], "held_low": ex["low"], "held_low_ms": ex["low_ms"]}
                        self.sessions.update_bucket(conn, b["id"], **fields)
                        b.update(fields)
        for t in trades:
            t["time_iso"] = _iso(t["time_ms"])
        out = []
        for b in buckets:
            live = self._excursion(s, b["opened_ms"], s["cursor_ms"]) if b["status"] == "open" and s["status"] == "ready" else None
            out.append(self._public_bucket(b, None, live, buckets))
        return {"trades": trades, "buckets": out}

    def _excursion(self, s: dict, from_ms: int, to_ms: int) -> dict | None:
        """Highest high and lowest low of the base candles opening between `from_ms` and `to_ms`
        (both included): what the market did while a bucket bought at `from_ms` was held."""
        return self.candles.extremes_between(s["symbol"], s["base_interval"], from_ms, to_ms)

    # ------------------------------------------------------------------ replay
    def step(self, session_id: str, interval: str | None = None, n: int = 1) -> dict:
        if n < 1:
            raise Invalid("n must be >= 1")
        with self._lock:
            s = self._get_ready(session_id)
            self._swing_only(s)
            base_ms = config.interval_ms(s["base_interval"])
            iv_ms = self._view_interval_ms(s, interval)
            cursor = s["cursor_ms"]
            if cursor < s["data_end_ms"]:
                if iv_ms == base_ms:
                    # The n-th stored base candle after the cursor (gaps are skipped by construction).
                    nxt = self.candles.nth_at_or_after(s["symbol"], s["base_interval"], cursor + base_ms, n)
                else:
                    # Advance n boundaries of the coarser interval, then land on the base candle
                    # that completes the last one (or the first stored candle after it, on a gap).
                    cur_close = cursor + base_ms
                    target = (cur_close // iv_ms + n) * iv_ms - base_ms
                    nxt = self.candles.first_at_or_after(s["symbol"], s["base_interval"], target)
                cursor = s["data_end_ms"] if nxt is None or nxt > s["data_end_ms"] else nxt
            auto = self._auto_sells(s, s["cursor_ms"], cursor) if cursor > s["cursor_ms"] else []
            with self.sessions.tx() as conn:
                self.sessions.update_session(conn, session_id, cursor_ms=cursor, updated_at=_now_iso())
        out = self.state(session_id)
        out["auto_sells"] = auto  # buckets the armed targets and stops sold during this step
        return out

    def reset(self, session_id: str) -> dict:
        with self._lock:
            s = self._get_ready(session_id)
            self._swing_only(s)
            self._wipe(s)
        return self.state(session_id)

    def _wipe(self, s: dict) -> None:
        """Back to the first candle with the starting capital: buckets, trades, fibs, journal rows
        and pictures gone. Shared with the day-trading engine's reset."""
        session_id = s["id"]
        with self.sessions.tx() as conn:
            self.sessions.delete_trades(conn, session_id)
            self.sessions.delete_buckets(conn, session_id)
            self.sessions.delete_fibs(conn, session_id)
            self.sessions.delete_snapshots(conn, session_id)
            self.sessions.update_session(
                conn, session_id, cursor_ms=s["data_start_ms"], cash=s["start_balance"], updated_at=_now_iso()
            )
        self._remove_snapshot_files(session_id)

    # ----------------------------------------------------------------- trading
    def buy(
        self, session_id: str, amount: float, interval: str | None = None, whole: bool = False, coins: int | None = None,
        sr_tol: float | None = None, reasons: list[str] | None = None, note: str | None = None,
        tp_pct: float | None = None, sl_pct: float | None = None,
    ) -> dict:
        """Open a bucket at the cursor price; `interval` is the timeframe whose RSI is recorded.
        `reasons` are the ticked keys of the buy checklist (see `reasons.py`) and `note` a free
        text; both go into the journal row with the checklist version. `tp_pct` and `sl_pct` are
        the take-profit and stop-loss distances in percent of the entry price; they are stored
        with the bucket and only act once the bucket is armed (see `arm`).

        With `whole`, the quantity `amount` would buy is rounded down to a whole number of coins
        and the cost (fee included) follows, so it never exceeds `amount`; fewer than one whole
        coin is refused. With `coins`, exactly that many whole coins are
        bought and `amount` is ignored: the cost is coins * price / (1 - fee rate).
        """
        checklist = self._checklist("buy", reasons, note)
        tp_pct, sl_pct = self._plan_pcts(tp_pct, sl_pct)
        with self._lock:
            s = self._get_ready(session_id)
            self._swing_only(s)
            rsi_iv = interval or s["base_interval"]
            self._view_interval_ms(s, rsi_iv)
            price = self._price(s)
            qty_whole: int | None = None
            if coins is not None:
                if not (isinstance(coins, int) and coins >= 1):
                    raise Invalid("coins must be a whole number of at least 1")
                qty_whole = coins
                amount = coins * price / (1 - s["fee_rate"])
                if amount > s["cash"] + 1e-9:
                    base = s["symbol"].removesuffix(config.QUOTE_ASSET)
                    raise Invalid(f"{coins} whole {base} cost {amount:.2f} {config.QUOTE_ASSET} with the fee, more than the cash {s['cash']:.2f}")
            amount = float(amount)
            if not (math.isfinite(amount) and amount >= MIN_ORDER_QUOTE):
                raise Invalid(f"amount must be at least {MIN_ORDER_QUOTE} {config.QUOTE_ASSET}")
            if amount > s["cash"] + 1e-9:
                raise Invalid(f"amount {amount:.2f} exceeds cash {s['cash']:.2f}")
            if whole and qty_whole is None:
                amount, qty_whole = self._whole_coins(s, amount, price)
            gauges = self._gauges(s, rsi_iv, sr_tol)
            rsi = gauges["rsi"]["value"]
            with self.sessions.tx() as conn:
                before = self.sessions.list_buckets(conn, session_id)
            ref_before = self._reference(s, before, [b for b in before if b["status"] == "closed"], price)
            fee = amount * s["fee_rate"]
            qty = float(qty_whole) if qty_whole is not None else (amount - fee) / price
            cash_after = s["cash"] - amount
            with self.sessions.tx() as conn:
                bucket = self.sessions.add_bucket(
                    conn, session_id=session_id, status="open", opened_ms=s["cursor_ms"],
                    entry_price=price, qty=qty, cost=amount, buy_fee=fee,
                    buy_rsi=rsi, buy_rsi_interval=rsi_iv, **self._long_avg_fields("buy", gauges),
                    tp_pct=tp_pct, sl_pct=sl_pct,
                    tp_price=price * (1 + tp_pct / 100.0) if tp_pct is not None else None,
                    sl_price=price * (1 - sl_pct / 100.0) if sl_pct is not None else None,
                )
                self.sessions.add_trade(
                    conn, session_id=session_id, bucket_id=bucket["id"], side="buy", time_ms=s["cursor_ms"],
                    price=price, qty=qty, quote=amount, fee=fee, cash_after=cash_after,
                )
                self.sessions.update_session(conn, session_id, cash=cash_after, updated_at=_now_iso())
            self._record_snapshot(session_id, bucket, "buy", rsi_iv, gauges, ref_before, checklist)
        return self._public_bucket(bucket, price, self._excursion(s, bucket["opened_ms"], s["cursor_ms"]))

    @staticmethod
    def _plan_pcts(tp_pct: float | None, sl_pct: float | None) -> tuple[float | None, float | None]:
        """Validated take-profit and stop-loss percentages (None = not set)."""
        if tp_pct is not None:
            tp_pct = float(tp_pct)
            if not (math.isfinite(tp_pct) and 0 < tp_pct <= TP_MAX_PCT):
                raise Invalid(f"the target must be between 0 and {TP_MAX_PCT:g} percent above the entry price")
        if sl_pct is not None:
            sl_pct = float(sl_pct)
            if not (math.isfinite(sl_pct) and 0 < sl_pct < SL_MAX_PCT):
                raise Invalid(f"the stop must be between 0 and {SL_MAX_PCT:g} percent below the entry price")
        return tp_pct, sl_pct

    def arm(self, session_id: str, bucket_id: int, armed: bool) -> dict:
        """Switch the automatic sell of an open bucket on or off. While armed, the replay sells
        everything left in the bucket when a base candle reaches the target (its high) or the
        stop (its low): see `step`. The replay time of switching on is kept on the bucket."""
        with self._lock:
            s = self._get_ready(session_id)
            self._swing_only(s)
            with self.sessions.tx() as conn:
                bucket = self.sessions.get_bucket(conn, bucket_id)
            if bucket is None or bucket["session_id"] != session_id:
                raise NotFound(f"bucket {bucket_id} not found in session {session_id}")
            if bucket["status"] != "open":
                raise Conflict(f"bucket #{bucket['seq']} is already closed")
            if armed and bucket.get("tp_price") is None and bucket.get("sl_price") is None:
                raise Invalid(f"bucket #{bucket['seq']} has no target and no stop to arm")
            with self.sessions.tx() as conn:
                self.sessions.update_bucket(conn, bucket_id, armed_ms=s["cursor_ms"] if armed else None)
                bucket = self.sessions.get_bucket(conn, bucket_id)
                buckets = self.sessions.list_buckets(conn, session_id)
            price = self._price(s)
        return self._public_bucket(bucket, price, self._excursion(s, bucket["opened_ms"], s["cursor_ms"]), buckets)  # type: ignore[arg-type]

    @staticmethod
    def _checklist(side: str, reasons: list[str] | None, note: str | None) -> dict:
        """Journal fields for the ticked reasons of a trade; unknown keys are refused."""
        try:
            ticked, note = clean_reasons(side, reasons, note)
        except ValueError as exc:
            raise Invalid(str(exc)) from None
        return {"reasons": ticked, "reason_note": note, "checklist": CHECKLIST_VERSION}

    @staticmethod
    def _long_avg_fields(side: str, gauges: dict) -> dict:
        """Bucket columns with the daily EMA 200 and the price's distance to it at a trade."""
        e = gauges["ema_long"]
        return {f"{side}_ema200": e["value"], f"{side}_ema200_pct": e["price_pct"]}

    @staticmethod
    def long_avg_changes(buy: dict, sell: dict) -> dict:
        """How the daily EMA 200 moved between a buy and a sell (percent of the value at the buy)
        and how the price's distance to it changed (percentage points: distance at the sell minus
        distance at the buy). `buy`/`sell` map ema200 and ema200_pct; None when a side lacks a value."""
        out: dict[str, Any] = {}
        for name in ("ema200",):
            b, s = buy.get(name), sell.get(name)
            out[f"{name}_change_pct"] = (s / b - 1.0) * 100.0 if b and s is not None else None
            bp, sp = buy.get(f"{name}_pct"), sell.get(f"{name}_pct")
            out[f"{name}_dist_change"] = sp - bp if bp is not None and sp is not None else None
        return out

    @staticmethod
    def whole_coin_order(amount: float, price: float, fee_rate: float, cash: float) -> tuple[float, int]:
        """(cost, coins) for the whole number of coins `amount` buys, rounded down, so the cost
        (fee included) stays within `amount` and `cash`. Zero coins means none is affordable."""
        target = min(amount, cash) * (1 - fee_rate) / price
        coins = math.floor(target + 1e-9)
        cost = coins * price / (1 - fee_rate)
        if cost > cash + 1e-9:  # the epsilon let one coin too many through
            coins -= 1
            cost = coins * price / (1 - fee_rate)
        return cost, max(coins, 0)

    def _whole_coins(self, s: dict, amount: float, price: float) -> tuple[float, int]:
        cost, coins = self.whole_coin_order(amount, price, s["fee_rate"], s["cash"])
        base = s["symbol"].removesuffix(config.QUOTE_ASSET)
        if coins < 1:
            one = price / (1 - s["fee_rate"])
            raise Invalid(f"one whole {base} costs {one:.2f} {config.QUOTE_ASSET} with the fee, more than the cash {s['cash']:.2f}")
        return cost, coins

    def sell(
        self, session_id: str, bucket_id: int, interval: str | None = None, sr_tol: float | None = None,
        part: float = 1.0, whole: bool = False, reasons: list[str] | None = None, note: str | None = None,
    ) -> dict:
        """Sell `part` (0 < part <= 1) of what is left in a bucket at the cursor price.
        `reasons` are the ticked keys of the sell checklist and `note` a free text (journal row).

        A part smaller than the whole bucket becomes its own closed row with the bucket's number
        and the next part number (a quarter of the remaining coins carries a quarter of the
        remaining cost and buy fee), while the bucket stays open with the rest; selling the rest
        closes the bucket itself as the last part. With `whole`, a part is rounded down to whole
        coins and refused when that leaves none. Returns the closed row."""
        checklist = self._checklist("sell", reasons, note)
        with self._lock:
            s = self._get_ready(session_id)
            self._swing_only(s)
            rsi_iv = interval or s["base_interval"]
            self._view_interval_ms(s, rsi_iv)
            with self.sessions.tx() as conn:
                bucket = self.sessions.get_bucket(conn, bucket_id)
            if bucket is None or bucket["session_id"] != session_id:
                raise NotFound(f"bucket {bucket_id} not found in session {session_id}")
            if bucket["status"] != "open":
                raise Conflict(f"bucket #{bucket['seq']} is already closed")
            part = float(part)
            if not (math.isfinite(part) and 0 < part <= 1):
                raise Invalid("part must be between 0 and 1 (1 = everything left in the bucket)")
            qty_sold = bucket["qty"] if part >= 1 else bucket["qty"] * part
            if whole and part < 1:
                qty_sold = float(math.floor(qty_sold + 1e-9))
                if qty_sold < 1:
                    base = s["symbol"].removesuffix(config.QUOTE_ASSET)
                    raise Invalid(f"{part * 100:g} % of the {qtyf(bucket['qty'])} {base} left in bucket #{bucket['seq']} is less than one whole coin")
            price = self._price(s)
            sold = self._execute_sell(s, bucket, qty_sold, price, rsi_iv, sr_tol, checklist)
        return self._public_bucket(sold, price)

    def _execute_sell(
        self, s: dict, bucket: dict, qty_sold: float, price: float, rsi_iv: str, sr_tol: float | None,
        checklist: dict, trigger: str | None = None,
    ) -> dict:
        """Close `qty_sold` coins of an open bucket at `price` at the session's cursor (the lock is
        held). `trigger` marks a sell the armed target ("target") or stop ("stop") fired. The
        journal row also records whether the target or the stop was reached while the bucket was
        held, whether it was armed or not. Updates `s["cash"]`; returns the closed row."""
        session_id, bucket_id = s["id"], bucket["id"]
        full = qty_sold >= bucket["qty"] * (1 - 1e-9)
        gauges = self._gauges(s, rsi_iv, sr_tol)
        rsi = gauges["rsi"]["value"]
        with self.sessions.tx() as conn:
            before = self.sessions.list_buckets(conn, session_id)
        ref_before = self._reference(s, before, [b for b in before if b["status"] == "closed"], price)
        parts_before = sum(1 for b in before if b.get("parent_id") == bucket_id)
        ex = self._excursion(s, bucket["opened_ms"], s["cursor_ms"]) or {}
        share = 1.0 if full else qty_sold / bucket["qty"]
        cost, buy_fee = bucket["cost"] * share, bucket["buy_fee"] * share
        proceeds = qty_sold * price
        fee = proceeds * s["fee_rate"]
        realized = proceeds - fee - cost
        cash_after = s["cash"] + proceeds - fee
        closing = dict(
            status="closed", closed_ms=s["cursor_ms"], exit_price=price, proceeds=proceeds, sell_fee=fee,
            realized_pnl=realized, sell_rsi=rsi, sell_rsi_interval=rsi_iv, **self._long_avg_fields("sell", gauges),
            held_high=ex.get("high"), held_high_ms=ex.get("high_ms"), held_low=ex.get("low"), held_low_ms=ex.get("low_ms"),
            trigger=trigger,
        )
        plan = {k: bucket.get(k) for k in ("tp_pct", "sl_pct", "tp_price", "sl_price", "armed_ms")}
        with self.sessions.tx() as conn:
            if full:
                if parts_before:
                    closing.update(part=parts_before + 1, part_pct=100.0)
                self.sessions.update_bucket(conn, bucket_id, **closing)
                sold = self.sessions.get_bucket(conn, bucket_id)
            else:
                sold = self.sessions.add_bucket(
                    conn, session_id=session_id, seq=bucket["seq"], opened_ms=bucket["opened_ms"],
                    entry_price=bucket["entry_price"], qty=qty_sold, cost=cost, buy_fee=buy_fee,
                    buy_rsi=bucket["buy_rsi"], buy_rsi_interval=bucket["buy_rsi_interval"],
                    **{f"buy_{k}": bucket.get(f"buy_{k}") for k in ("ema200", "ema200_pct")},
                    part=parts_before + 1, part_pct=share * 100.0, parent_id=bucket_id, **plan, **closing,
                )
                self.sessions.update_bucket(
                    conn, bucket_id, qty=bucket["qty"] - qty_sold, cost=bucket["cost"] - cost, buy_fee=bucket["buy_fee"] - buy_fee,
                )
            self.sessions.add_trade(
                conn, session_id=session_id, bucket_id=sold["id"], side="sell", time_ms=s["cursor_ms"],
                price=price, qty=qty_sold, quote=proceeds, fee=fee, cash_after=cash_after,
            )
            self.sessions.update_session(conn, session_id, cash=cash_after, updated_at=_now_iso())
        s["cash"] = cash_after
        hit = self.candles.first_cross(s["symbol"], s["base_interval"], bucket["opened_ms"], s["cursor_ms"], plan["tp_price"], plan["sl_price"])
        extra = {
            **checklist,
            "trigger": trigger,
            "armed_at": _iso(plan["armed_ms"]) if plan["armed_ms"] is not None else None,
            "target_hit": hit["above_ms"] is not None if plan["tp_price"] is not None else None,
            "target_hit_at": _iso(hit["above_ms"]) if hit["above_ms"] is not None else None,
            "stop_hit": hit["below_ms"] is not None if plan["sl_price"] is not None else None,
            "stop_hit_at": _iso(hit["below_ms"]) if hit["below_ms"] is not None else None,
        }
        self._record_snapshot(session_id, sold, "sell", rsi_iv, gauges, ref_before, extra)  # type: ignore[arg-type]
        return sold  # type: ignore[return-value]

    def _auto_sells(self, s: dict, from_ms: int, to_ms: int) -> list[dict]:
        """Fire the armed targets and stops of the open buckets on the base candles opening in
        (from_ms, to_ms]: the first candle whose high reaches the target or whose low reaches the
        stop sells everything left in the bucket at that level, or at the candle's open when it
        already opened beyond the level (a gap). A candle reaching both counts as the stop. Each
        sell happens with the cursor on its candle, in time order; the cursor is left on the last
        one (the caller moves it on). Returns the closed rows."""
        with self.sessions.tx() as conn:
            armed = [
                b for b in self.sessions.list_buckets(conn, s["id"], "open")
                if b.get("armed_ms") is not None and (b.get("tp_price") is not None or b.get("sl_price") is not None)
            ]
        events: list[tuple[int, str, int]] = []
        for b in armed:
            x = self.candles.first_cross(s["symbol"], s["base_interval"], from_ms + 1, to_ms, b.get("tp_price"), b.get("sl_price"))
            hits = [(ms, kind) for ms, kind in ((x["below_ms"], "stop"), (x["above_ms"], "target")) if ms is not None]
            if hits:
                ms, kind = min(hits)  # the earliest candle; on the same candle "stop" sorts first
                events.append((ms, kind, b["id"]))
        sold: list[dict] = []
        for ms, kind, bucket_id in sorted(events):
            with self.sessions.tx() as conn:
                bucket = self.sessions.get_bucket(conn, bucket_id)
            if bucket is None or bucket["status"] != "open":
                continue
            with self.sessions.tx() as conn:
                self.sessions.update_session(conn, s["id"], cursor_ms=ms)
            s["cursor_ms"] = ms
            candle = self.candles.get_candles(s["symbol"], s["base_interval"], ms, ms)
            open_ = float(candle["open"].iloc[0])
            level = bucket["tp_price"] if kind == "target" else bucket["sl_price"]
            fill = max(level, open_) if kind == "target" else min(level, open_)
            extra = {"reasons": [], "reason_note": None, "checklist": CHECKLIST_VERSION}
            row = self._execute_sell(s, bucket, bucket["qty"], fill, s["base_interval"], None, extra, trigger=kind)
            sold.append(self._public_bucket(row, fill))
        return sold

    # -------------------------------------------------------------------- view
    def view(
        self,
        session_id: str,
        interval: str | None = None,
        limit: int = 500,
        indicators: list[tuple[str, dict[str, Any]]] | None = None,
        strategy: tuple[str, dict[str, Any]] | None = None,
        auto_fib: int | None = None,
        fvg_min_pct: float | None = None,
        daily_ema: bool = False,
        sr_tol: float | None = None,
        rsi_panes: list[str] | None = None,
    ) -> dict:
        """Candles at `interval` known at the cursor, with indicators and signals.

        `rsi_panes` names timeframes whose RSI(14) is returned as well, one value per bar on
        screen (see `_rsi_pane`), for extra panes under the chart.

        Never returns a candle whose close lies after the cursor's base candle.
        Indicators are computed over the last `limit + COMPUTE_WARMUP_BARS` bars (never
        before the session's download anchor), so every bar's value stays stable as the
        cursor advances, at constant cost per step.
        """
        if limit < 1:
            raise Invalid("limit must be >= 1")
        panes = list(dict.fromkeys(rsi_panes or []))   # duplicates would only cost time
        if len(panes) > RSI_PANES_MAX:
            raise Invalid(f"at most {RSI_PANES_MAX} RSI panes")
        s = self._get_ready(session_id)
        for pane_iv in panes:
            self._view_interval_ms(s, pane_iv)   # unknown, or finer than the base: Invalid before any work
        base_ms = config.interval_ms(s["base_interval"])
        iv = interval or s["base_interval"]
        iv_ms = self._view_interval_ms(s, iv)
        cursor = s["cursor_ms"]
        cur_close = cursor + base_ms
        anchor = s["start_ms"] - WARMUP_BARS * iv_ms

        df = self.candles.get_candles(s["symbol"], iv, anchor, cur_close - iv_ms, limit=limit + COMPUTE_WARMUP_BARS)
        partial_open = floor_to_slot(cursor, iv_ms)
        partial = partial_open + iv_ms > cur_close
        if any(key == "vdelta" for key, _ in indicators or []):
            # candles stored before taker-buy volumes were kept get them in the background, also
            # the coming ones (see TAKER_FILL_AHEAD); bars on screen stay empty until a later view
            if len(df):
                shown = df["time"].tail(limit)
                last = int(shown.iloc[-1]) * 1000
                self.candles.fill_taker_buy_soon(s["symbol"], iv, int(shown.iloc[0]) * 1000, last + TAKER_CHECK_AHEAD * iv_ms,
                                                 fill_end_ms=last + TAKER_FILL_AHEAD * iv_ms)
            if partial:   # the forming candle is summed from base candles: fill those ahead as well
                self.candles.fill_taker_buy_soon(s["symbol"], s["base_interval"], partial_open,
                                                 cursor + TAKER_CHECK_AHEAD * base_ms, fill_end_ms=cursor + TAKER_FILL_AHEAD * base_ms)
        if partial:
            base = self.candles.get_candles(s["symbol"], s["base_interval"], partial_open, cursor)
            if len(base):
                row = pd.DataFrame([aggregate(base, partial_open // 1000)])
                df = pd.concat([df, row], ignore_index=True) if len(df) else row
            else:
                partial = False
        try:
            payload = build_payload(df, indicators or [], strategy, limit)
        except ValueError as exc:
            raise Invalid(str(exc)) from None
        payload.update(
            {
                "symbol": s["symbol"],
                "interval": iv,
                "cursor_ms": cursor,
                "cursor_iso": _iso(cursor),
                "partial_last": bool(partial and payload["count"] > 0),
                "gauges": self._gauges(s, iv, sr_tol),
                "auto_fib": self._auto_fib(df, iv, auto_fib) if auto_fib else None,
                "fvg": fair_value_gaps(df.tail(limit + 2), fvg_min_pct) if fvg_min_pct is not None else None,
                "daily_ema": self._daily_ema_overlay(s, iv, payload["time"]) if daily_ema else None,
                "rsi_panes": [self._rsi_pane(s, pane_iv, iv_ms, payload["time"], cur_close) for pane_iv in panes],
            }
        )
        return payload

    def _rsi_pane(self, s: dict, interval: str, view_ms: int, times: list[int], cur_close: int) -> dict:
        """RSI(14) of `interval`, one value per bar on screen (an extra pane under the chart).

        A bar shows the RSI of the last `interval` candle that had closed by the bar's own close,
        so no value uses a close from the bar's future; the latest bar shows the forming candle's
        RSI, exactly as a chart of `interval` would at the cursor. Bars before the first usable
        RSI value carry None.
        """
        pane_ms = self._view_interval_ms(s, interval)
        out = {"interval": interval, "period": RSI_PERIOD, "values": [None] * len(times)}
        if not times:
            return out
        span = (times[-1] - times[0]) * 1000 // pane_ms + 2
        df = self._candles_at_cursor(s, interval, int(span) + RSI_WARMUP_BARS)
        if len(df) <= RSI_PERIOD:
            return out
        rsi = core.rsi(df["close"], RSI_PERIOD).to_numpy(dtype=float)
        # when each pane candle became known: its close, or the cursor for the forming one
        known = np.minimum(df["time"].to_numpy(dtype=np.int64) * 1000 + pane_ms, cur_close)
        closes = np.minimum(np.asarray(times, dtype=np.int64) * 1000 + view_ms, cur_close)
        idx = np.searchsorted(known, closes, side="right") - 1
        values: list[float | None] = []
        for i in idx:
            v = rsi[i] if i >= 0 else math.nan
            values.append(float(v) if math.isfinite(v) else None)
        out["values"] = values
        return out

    @staticmethod
    def _auto_fib(df: pd.DataFrame, interval: str, lookback: int) -> dict | None:
        """Retracement of the last `lookback` candles: from their extreme low to extreme high.

        The later extreme is the 0 % anchor (the swing end), the earlier one the 100 % anchor.
        """
        if not (1 < lookback <= AUTO_FIB_MAX_LOOKBACK):
            raise Invalid(f"auto_fib lookback must be between 2 and {AUTO_FIB_MAX_LOOKBACK}")
        tail = df.tail(lookback)
        if len(tail) < 2:
            return None
        i_hi = int(tail["high"].values.argmax())
        i_lo = int(tail["low"].values.argmin())
        hi_t, hi_p = int(tail["time"].iloc[i_hi]) * 1000, float(tail["high"].iloc[i_hi])
        lo_t, lo_p = int(tail["time"].iloc[i_lo]) * 1000, float(tail["low"].iloc[i_lo])
        if hi_p == lo_p:
            return None
        up = lo_t <= hi_t  # low first, then high: an up-swing retraces downwards from the high
        t1, p1, t2, p2 = (lo_t, lo_p, hi_t, hi_p) if up else (hi_t, hi_p, lo_t, lo_p)
        return {
            "interval": interval, "lookback": lookback, "bars": int(len(tail)),
            "t1_ms": t1, "p1": p1, "t2_ms": t2, "p2": p2, "t1_iso": _iso(t1), "t2_iso": _iso(t2),
            "direction": "up" if up else "down", "levels": fib_levels(p1, p2),
        }

    def _daily_ema_overlay(self, s: dict, interval: str, times: list[int]) -> dict:
        """EMA 200 of daily candles, one value per bar on screen (the same series as the gauge).

        On the daily view it is the ordinary EMA. On finer views each bar shows the value of the
        last COMPLETED day before it, so the line never uses a close that lies in the bar's future.
        """
        day_ms = config.interval_ms(EMA_LONG_INTERVAL)
        out = {"period": EMA_LONG_PERIOD, "interval": EMA_LONG_INTERVAL, "values": [None] * len(times)}
        if not times or config.interval_ms(interval) > day_ms:
            return out
        daily = self._candles_at_cursor(s, EMA_LONG_INTERVAL, EMA_LONG_WARMUP_BARS)
        if len(daily) < EMA_LONG_PERIOD:
            return out
        ema = core.ema(daily["close"], EMA_LONG_PERIOD)
        by_day = {int(t) * 1000: (float(v) if math.isfinite(v) else None) for t, v in zip(daily["time"], ema)}
        same_day = config.interval_ms(interval) == day_ms
        values = []
        for t in times:
            day = (t * 1000 // day_ms) * day_ms
            values.append(by_day.get(day if same_day else day - day_ms))
        out["values"] = values
        return out

    # -------------------------------------------------------------------- fibs
    def btc_context(
        self, session_id: str, years: int = 2, tol_pct: float = SR_TOL_PCT,
        indicators: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> dict:
        """Bitcoin daily candles of the last `years` years known at the cursor, with major levels.

        Whatever coin the session trades, the candles are BTCUSDT (downloaded on demand) and end
        with the replay day, still forming, aggregated from finer Bitcoin candles up to the cursor.
        Levels come from `support_resistance` over the closed days of the window.
        """
        if not (1 <= years <= CONTEXT_YEARS_MAX):
            raise Invalid(f"years must be between 1 and {CONTEXT_YEARS_MAX}")
        s = self._get_ready(session_id)
        base_ms = config.interval_ms(s["base_interval"])
        day_ms = config.interval_ms(CONTEXT_INTERVAL)
        cursor = s["cursor_ms"]
        cur_close = cursor + base_ms
        partial_open = floor_to_slot(cursor, day_ms)
        start = partial_open - years * 365 * day_ms
        sym = CONTEXT_SYMBOL
        own = s["symbol"] == sym
        # Finer candles for the forming day: the session's own when it trades Bitcoin, else hourly.
        fine = s["base_interval"] if own or base_ms >= config.interval_ms("1h") else "1h"
        fine_ms = config.interval_ms(fine)
        try:
            self.candles.ensure_range(sym, CONTEXT_INTERVAL, start - CONTEXT_WARMUP_DAYS * day_ms, partial_open - day_ms)
            if not own and partial_open + day_ms > cur_close and cur_close - fine_ms >= partial_open:
                self.candles.ensure_range(sym, fine, partial_open, cur_close - fine_ms)
        except Exception as exc:  # network, exchange or disk trouble: the peek simply fails this time
            raise Conflict(f"Bitcoin candles could not be downloaded: {exc}") from exc
        df = self.candles.get_candles(sym, CONTEXT_INTERVAL, start - CONTEXT_WARMUP_DAYS * day_ms, cur_close - day_ms)
        partial = partial_open + day_ms > cur_close
        if partial:
            fine_df = self.candles.get_candles(sym, fine, partial_open, cur_close - fine_ms)
            if len(fine_df):
                df = pd.concat([df, pd.DataFrame([aggregate(fine_df, partial_open // 1000)])], ignore_index=True)
            else:
                partial = False
        limit = int((df["time"] * 1000 >= start).sum()) if len(df) else 0
        if limit < 1:
            raise Conflict("no Bitcoin daily candles in the window")
        try:
            payload = build_payload(df, indicators or [], None, limit)
        except ValueError as exc:
            raise Invalid(str(exc)) from None
        closed = df.iloc[:-1] if partial else df
        closed = closed[closed["time"] * 1000 >= start]
        price = float(df["close"].iloc[-1])
        payload.update(
            {
                "symbol": sym, "interval": CONTEXT_INTERVAL, "cursor_ms": cursor, "cursor_iso": _iso(cursor),
                "partial_last": bool(partial and payload["count"] > 0), "years": years, "tol_pct": tol_pct,
                "window_start_ms": start, "window_start_iso": _iso(start), "price": price,
                "levels": support_resistance(closed, price, tol_pct),
            }
        )
        return payload

    def fibs(self, session_id: str) -> list[dict]:
        with self.sessions.tx() as conn:
            if self.sessions.get_session(conn, session_id) is None:
                raise NotFound(f"session {session_id} not found")
            rows = self.sessions.list_fibs(conn, session_id)
        return [self._public_fib(f) for f in rows]

    def add_fib(self, session_id: str, interval: str | None, t1_ms: int, p1: float, t2_ms: int, p2: float) -> dict:
        """Store a retracement; anchors must lie in the past of the replay cursor."""
        with self._lock:
            s = self._get_ready(session_id)
            iv = interval or s["base_interval"]
            self._view_interval_ms(s, iv)
            p1, p2 = float(p1), float(p2)
            if not (math.isfinite(p1) and math.isfinite(p2) and p1 > 0 and p2 > 0):
                raise Invalid("fib prices must be positive finite numbers")
            if p1 == p2:
                raise Invalid("the two fib anchors need different prices")
            t1_ms, t2_ms = int(t1_ms), int(t2_ms)
            for t in (t1_ms, t2_ms):
                if t < EARLIEST_START_MS:
                    raise Invalid("fib anchor time is before the data era")
                if t > s["cursor_ms"]:
                    raise Invalid("fib anchors cannot lie after the replay cursor")
            with self.sessions.tx() as conn:
                f = self.sessions.add_fib(
                    conn, session_id=session_id, interval=iv, t1_ms=t1_ms, p1=p1, t2_ms=t2_ms, p2=p2,
                    created_at=_now_iso(),
                )
        return self._public_fib(f)

    def delete_fib(self, session_id: str, fib_id: int) -> None:
        with self.sessions.tx() as conn:
            if self.sessions.get_session(conn, session_id) is None:
                raise NotFound(f"session {session_id} not found")
            if not self.sessions.delete_fib(conn, session_id, int(fib_id)):
                raise NotFound(f"fib {fib_id} not found in session {session_id}")

    @staticmethod
    def _public_fib(f: dict) -> dict:
        out = dict(f)
        out["t1_iso"] = _iso(f["t1_ms"])
        out["t2_iso"] = _iso(f["t2_ms"])
        out["direction"] = "up" if f["p2"] > f["p1"] else "down"
        out["levels"] = fib_levels(f["p1"], f["p2"])
        return out

    def _gauges(self, s: dict, interval: str, sr_tol: float | None = None) -> dict:
        """Side-panel readings at the cursor: RSI(14) and Bollinger of `interval`, EMA/SMA 200 of
        daily candles, the distance to the daily highs and lows (all-time, 1 year, 3 months) and the
        next support and resistance of the last year's daily candles."""
        price = self._price(s)
        history, complete = self._daily_history(s)
        df = self._candles_at_cursor(s, interval, RSI_WARMUP_BARS)
        rsi = self._rsi_from_df(df)
        bb = self._bollinger_from_df(df, price)
        ema = None
        if config.interval_ms(EMA_LONG_INTERVAL) >= config.interval_ms(s["base_interval"]):
            daily = self._candles_at_cursor(s, EMA_LONG_INTERVAL, EMA_LONG_WARMUP_BARS)
            if len(daily) >= EMA_LONG_PERIOD:
                value = float(core.ema(daily["close"], EMA_LONG_PERIOD).iloc[-1])
                ema = value if math.isfinite(value) else None
        return {
            "price": price,
            "rsi": {"value": rsi, "period": RSI_PERIOD, "interval": interval},
            "bb": {"period": BB_PERIOD, "mult": BB_MULT, "interval": interval, **bb},
            "ema_long": {
                "value": ema, "period": EMA_LONG_PERIOD, "interval": EMA_LONG_INTERVAL,
                "price_pct": (price / ema - 1.0) * 100.0 if ema else None,
            },
            "extremes": self._extremes_gauge(s, price, history, complete),
            "levels": self._levels_gauge(s, price, history, sr_tol),
        }

    # ------------------------------------------------------------ highs & lows
    def _daily_history(self, s: dict) -> tuple[pd.DataFrame, bool]:
        """Every daily candle of the session's coin known at the cursor, from its Binance listing to
        the forming day (aggregated from the base candles up to the cursor).

        The session's preparation only fetched the warm-up; the days before it are downloaded here,
        once per coin. When that fails the stored days are used, the second value is False and a
        retry waits HISTORY_RETRY_SECONDS so a dead network does not slow every step."""
        sym = s["symbol"]
        day_ms = config.interval_ms(EXTREMES_INTERVAL)
        need = floor_to_slot(s["cursor_ms"], day_ms) - day_ms  # last closed day
        if self._history_until.get(sym, HISTORY_START_MS - day_ms) < need and time.monotonic() >= self._history_retry.get(sym, 0.0):
            try:
                self.candles.ensure_range(sym, EXTREMES_INTERVAL, HISTORY_START_MS, need)
            except Exception as exc:  # network, exchange or disk trouble: show what is stored
                log.warning("daily history of %s could not be completed: %s", sym, exc)
                self._history_retry[sym] = time.monotonic() + HISTORY_RETRY_SECONDS
            else:
                # Preparing the session covered its warm-up to its end, which joins on to this range.
                self._history_until[sym] = max(need, floor_to_slot(s["end_ms"], day_ms) - day_ms)
        complete = self._history_until.get(sym, HISTORY_START_MS - day_ms) >= need
        return self._candles_at_cursor(s, EXTREMES_INTERVAL, None, anchor_ms=HISTORY_START_MS), complete

    def _extremes_gauge(self, s: dict, price: float, daily: pd.DataFrame, complete: bool) -> dict:
        day_ms = config.interval_ms(EXTREMES_INTERVAL)
        out = self.extremes(daily, price, floor_to_slot(s["cursor_ms"], day_ms))
        out["interval"] = EXTREMES_INTERVAL
        out["complete"] = complete
        return out

    def _levels_gauge(self, s: dict, price: float, daily: pd.DataFrame, sr_tol: float | None) -> dict:
        """Next support and resistance from the closed daily candles of the last NEAR_LEVEL_DAYS days."""
        day_ms = config.interval_ms(EXTREMES_INTERVAL)
        day_open = floor_to_slot(s["cursor_ms"], day_ms)
        t = daily["time"] * 1000
        closed = daily[(t >= day_open - NEAR_LEVEL_DAYS * day_ms) & (t < day_open)]
        out = nearest_levels(closed, price, SR_TOL_PCT if sr_tol is None else sr_tol)
        out.update({"interval": EXTREMES_INTERVAL, "days": NEAR_LEVEL_DAYS, "bars": int(len(closed)),
                    "from_iso": _iso(int(closed["time"].iloc[0]) * 1000) if len(closed) else None})
        return out

    @staticmethod
    def extremes(daily: pd.DataFrame, price: float, day_open_ms: int) -> dict:
        """Highest high and lowest low (wicks included) of daily candles over the EXTREME_WINDOWS,
        each ending with the replay day that opens at `day_open_ms`, and where `price` sits.

        A window of N days holds the replay day and the N days before it. `daily` holds the days
        known at the cursor, the forming one included, so the price lies between low and high and
        `vs_high_pct` <= 0 <= `vs_low_pct`. `position` is 0 at the low and 1 at the high. Ties go
        to the earliest day. Fewer days than the window asks for are used as they are."""
        day_ms = config.interval_ms(EXTREMES_INTERVAL)
        empty = {"bars": 0, "from_ms": None, "from_iso": None, "high": None, "high_ms": None, "high_iso": None,
                 "low": None, "low_ms": None, "low_iso": None, "vs_high_pct": None, "vs_low_pct": None, "position": None}
        out: dict[str, Any] = {}
        for key, days in EXTREME_WINDOWS:
            win = daily if days is None else daily[daily["time"] * 1000 >= day_open_ms - days * day_ms]
            if not len(win) or not (math.isfinite(price) and price > 0):
                out[key] = {"days": days, **empty}
                continue
            hi = win["high"].to_numpy(dtype=float)
            lo = win["low"].to_numpy(dtype=float)
            t = win["time"].to_numpy()
            i_hi, i_lo = int(hi.argmax()), int(lo.argmin())
            high, low = float(hi[i_hi]), float(lo[i_lo])
            out[key] = {
                "days": days, "bars": int(len(win)),
                "from_ms": int(t[0]) * 1000, "from_iso": _iso(int(t[0]) * 1000),
                "high": high, "high_ms": int(t[i_hi]) * 1000, "high_iso": _iso(int(t[i_hi]) * 1000),
                "low": low, "low_ms": int(t[i_lo]) * 1000, "low_iso": _iso(int(t[i_lo]) * 1000),
                "vs_high_pct": (price / high - 1.0) * 100.0 if high > 0 else None,
                "vs_low_pct": (price / low - 1.0) * 100.0 if low > 0 else None,
                "position": min(1.0, max(0.0, (price - low) / (high - low))) if high > low else 1.0,
            }
        return out

    # ----------------------------------------------------------------- journal
    @staticmethod
    def _snapshot_dir() -> Path:
        return config.DATA_DIR / "snapshots"

    def _remove_snapshot_files(self, session_id: str) -> None:
        shutil.rmtree(self._snapshot_dir() / session_id, ignore_errors=True)

    def _record_snapshot(
        self, session_id: str, bucket: dict, side: str, interval: str, gauges: dict, ref_before: dict, checklist: dict | None = None,
    ) -> None:
        """Write the journal row for a trade just committed: readings at that moment, no picture yet.
        `checklist` adds the ticked reasons, the note and the checklist version."""
        st = self.state(session_id)
        acct = st["account"]
        bb, ema = gauges["bb"], gauges["ema_long"]
        is_sell = side == "sell"
        r: dict[str, Any] = {
            "side": side,
            "bucket_seq": bucket["seq"],
            "part": bucket.get("part"),
            "part_pct": bucket.get("part_pct"),
            "time_iso": _iso(bucket["closed_ms"] if is_sell else bucket["opened_ms"]),
            "interval": interval,
            "price": bucket["exit_price"] if is_sell else bucket["entry_price"],
            "qty": bucket["qty"],
            "quote": bucket["proceeds"] if is_sell else bucket["cost"],
            "fee": bucket["sell_fee"] if is_sell else bucket["buy_fee"],
            "tp_pct": bucket.get("tp_pct"),
            "sl_pct": bucket.get("sl_pct"),
            "tp_price": bucket.get("tp_price"),
            "sl_price": bucket.get("sl_price"),
            "rsi": gauges["rsi"]["value"],
            "bb_pctb": bb["pctb"],
            "bb_width_pct": bb["width_pct"],
            "bb_upper": bb["upper"],
            "bb_lower": bb["lower"],
            "ema200": ema["value"],
            "ema200_pct": ema["price_pct"],
            **self._extremes_readings(gauges["extremes"]),
            **self._levels_readings(gauges["levels"]),
            "vs_last_buy_pct": ref_before["last_buy"]["pct"] if ref_before["last_buy"] else None,
            "vs_last_buy_basis": ref_before["last_buy"]["basis"] if ref_before["last_buy"] else None,
            "vs_last_sell_pct": ref_before["last_sell"]["pct"] if ref_before["last_sell"] else None,
            "vs_last_sell_basis": ref_before["last_sell"]["basis"] if ref_before["last_sell"] else None,
            "cash_after": acct["cash"],
            "equity_after": acct["equity"],
            "open_buckets_after": acct["open_buckets"],
            **(checklist or {}),
        }
        if is_sell:
            held = bucket["closed_ms"] - bucket["opened_ms"]
            r.update({
                "realized_pnl": bucket["realized_pnl"],
                "realized_pnl_pct": bucket["realized_pnl"] / bucket["cost"] * 100.0,
                "held_ms": held,
                "held_hours": held / 3_600_000,
                "held_high": bucket.get("held_high"),
                "held_high_pct": (bucket["held_high"] / bucket["entry_price"] - 1.0) * 100.0 if bucket.get("held_high") else None,
                "held_high_time": _iso(bucket["held_high_ms"]) if bucket.get("held_high_ms") is not None else None,
                "held_low": bucket.get("held_low"),
                "held_low_pct": (bucket["held_low"] / bucket["entry_price"] - 1.0) * 100.0 if bucket.get("held_low") else None,
                "held_low_time": _iso(bucket["held_low_ms"]) if bucket.get("held_low_ms") is not None else None,
            })
        with self.sessions.tx() as conn:
            if is_sell:
                prev = self.sessions.get_snapshot(conn, session_id, bucket.get("parent_id") or bucket["id"], "buy")
                r["at_buy"] = json.loads(prev["readings"]) if prev else None
                r.update(self.long_avg_changes(r["at_buy"] or {}, r))
            existing = self.sessions.get_snapshot(conn, session_id, bucket["id"], side)
            fields = dict(time_ms=bucket["closed_ms"] if is_sell else bucket["opened_ms"], interval=interval, readings=json.dumps(r))
            if existing:
                self.sessions.update_snapshot(conn, existing["id"], **fields)
            else:
                self.sessions.add_snapshot(conn, session_id=session_id, bucket_id=bucket["id"], side=side,
                                           image_path=None, created_at=_now_iso(), **fields)

    @staticmethod
    def _levels_readings(lv: dict) -> dict:
        """Flat journal fields for the next resistance and support zones: centre, edges, distance to the
        nearer edge, touches, breaks, first and last touch."""
        r: dict[str, Any] = {}
        for key, name in (("resistance", "next_resistance"), ("support", "next_support")):
            l = lv.get(key) or {}
            r[name] = l.get("price")
            r[f"{name}_low"] = l.get("low")
            r[f"{name}_high"] = l.get("high")
            r[f"{name}_pct"] = l.get("pct")
            r[f"{name}_touches"] = l.get("touches")
            r[f"{name}_breaks"] = l.get("breaks")
            r[f"{name}_first"] = l["first_iso"][:10] if l.get("first_iso") else None
            r[f"{name}_last"] = l["last_iso"][:10] if l.get("last_iso") else None
        return r

    @staticmethod
    def _extremes_readings(ex: dict) -> dict:
        """Flat journal fields: price of, distance to and date of the high and low of each window."""
        r: dict[str, Any] = {}
        for key, (hk, lk) in EXTREME_JOURNAL_KEYS.items():
            w = ex.get(key) or {}
            for name, side in ((hk, "high"), (lk, "low")):
                r[name] = w.get(side)
                r[f"{name}_pct"] = w.get(f"vs_{side}_pct")
                iso = w.get(f"{side}_iso")
                r[f"{name}_date"] = iso[:10] if iso else None
        return r

    def snapshots(self, session_id: str) -> list[dict]:
        with self.sessions.tx() as conn:
            if self.sessions.get_session(conn, session_id) is None:
                raise NotFound(f"session {session_id} not found")
            rows = self.sessions.list_snapshots(conn, session_id)
        return [self._public_snapshot(r) for r in rows]

    def attach_snapshot_image(self, session_id: str, bucket_id: int, side: str, image: str, extra: dict | None) -> dict:
        """Store the chart picture (PNG data URL) for a journal row and merge UI-side readings."""
        if side not in ("buy", "sell"):
            raise Invalid("side must be 'buy' or 'sell'")
        data = decode_png_data_url(image)
        extra = extra or {}
        for k, v in extra.items():
            if not isinstance(k, str) or not isinstance(v, (str, int, float, bool, type(None))):
                raise Invalid("extra readings must map names to simple values")
        if len(json.dumps(extra)) > SNAPSHOT_EXTRA_MAX_CHARS:
            raise Invalid("extra readings too large")
        with self._lock:
            with self.sessions.tx() as conn:
                if self.sessions.get_session(conn, session_id) is None:
                    raise NotFound(f"session {session_id} not found")
                snap = self.sessions.get_snapshot(conn, session_id, int(bucket_id), side)
                if snap is None:
                    raise NotFound(f"no {side} journal row for bucket {bucket_id}")
                rel = f"{session_id}/{int(bucket_id)}_{side}.png"
                path = self._snapshot_dir() / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                readings = json.loads(snap["readings"])
                readings.update(extra)
                self.sessions.update_snapshot(conn, snap["id"], image_path=rel, readings=json.dumps(readings))
                snap = self.sessions.get_snapshot_by_id(conn, session_id, snap["id"])
        return self._public_snapshot(snap)  # type: ignore[arg-type]

    def snapshot_image_path(self, session_id: str, snap_id: int) -> Path:
        with self.sessions.tx() as conn:
            snap = self.sessions.get_snapshot_by_id(conn, session_id, int(snap_id))
        if snap is None:
            raise NotFound(f"snapshot {snap_id} not found in session {session_id}")
        if not snap["image_path"]:
            raise NotFound(f"snapshot {snap_id} has no picture")
        path = self._snapshot_dir() / snap["image_path"]
        if not path.is_file():
            raise NotFound(f"picture file for snapshot {snap_id} is missing")
        return path

    def snapshots_csv(self, session_id: str) -> str:
        """All journal rows as CSV; nested at-buy readings of a sell are flattened to at_buy_* columns."""
        rows = [self._flatten_readings(s) for s in self.snapshots(session_id)]
        base = ["id", "bucket_id", "side", "time_iso", "interval"]
        keys: list[str] = []
        for r in rows:
            for k in r:
                if k not in base and k not in keys:
                    keys.append(k)
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=base + keys, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return out.getvalue()

    @staticmethod
    def _flatten_readings(snap: dict) -> dict:
        flat: dict[str, Any] = {"id": snap["id"], "bucket_id": snap["bucket_id"], "side": snap["side"],
                                "time_iso": snap["time_iso"], "interval": snap["interval"]}
        cell = lambda v: " | ".join(str(x) for x in v) if isinstance(v, list) else v  # noqa: E731
        for k, v in snap["readings"].items():
            if k == "at_buy":
                for k2, v2 in (v or {}).items():
                    flat[f"at_buy_{k2}"] = cell(v2)
            else:
                flat[k] = cell(v)
        flat["has_image"] = snap["has_image"]
        return flat

    # ------------------------------------------------------------- performance
    def performance(self, session_id: str) -> dict:
        """Strategy-tester style report of one session: numbers, equity curve at every base
        candle (open buckets at market), drawdown, trades and monthly returns (performance.py)."""
        st = self.state(session_id)
        with self.sessions.tx() as conn:
            s = self.sessions.get_session(conn, session_id)
            buckets = self.sessions.list_buckets(conn, session_id)
            trades = self.sessions.list_trades(conn, session_id)
        start = s["data_start_ms"] or s["start_ms"]  # type: ignore[index]
        candles = None
        if s["status"] == "ready" and s["cursor_ms"] is not None:  # type: ignore[index]
            candles = self.candles.get_candles(s["symbol"], s["base_interval"], start, s["cursor_ms"])  # type: ignore[index]
        return json_safe(session_report(s, buckets, trades, candles, st["account"]))  # type: ignore[arg-type]

    def performance_all(self) -> dict:
        """The report over every session: all closed trades together, the realized P&L added up
        trade by trade as the curve, months by closing date."""
        with self.sessions.tx() as conn:
            data = [(s, self.sessions.list_buckets(conn, s["id"])) for s in self.sessions.list_sessions(conn)]
        parts = []
        for s, buckets in data:
            try:
                account = self.state(s["id"])["account"]
            except (NotFound, Conflict):
                account = None
            parts.append((s, buckets, account))
        return json_safe(combined_report(parts))

    # ----------------------------------------------------------------- reasons
    def reason_stats(self, symbol: str | None = None) -> dict:
        """Reason statistics over every session, or one coin: closed trades (a buy with all its
        sells, P&L in percent of the whole cost) for the buy reasons and every sell (percent of
        the part's cost) for the sell reasons. Only rows logged with a checklist count; older
        rows are reported as `unlisted`. A trade counts once its bucket is fully closed."""
        trades: list[dict] = []
        sells: list[dict] = []
        unlisted = 0
        symbols: set[str] = set()
        with self.sessions.tx() as conn:
            data = [
                (s, self.sessions.list_buckets(conn, s["id"]), self.sessions.list_snapshots(conn, s["id"]))
                for s in self.sessions.list_sessions(conn)
            ]
        for s, buckets, snaps in data:
            symbols.add(s["symbol"])
            if symbol and s["symbol"] != symbol:
                continue
            readings: dict[tuple[int, str], dict] = {}
            for r in snaps:
                try:
                    readings[(r["bucket_id"], r["side"])] = json.loads(r["readings"])
                except (TypeError, ValueError):
                    continue
            parts_of: dict[int, list[dict]] = {}
            for b in buckets:
                if b.get("parent_id"):
                    parts_of.setdefault(b["parent_id"], []).append(b)
            for b in buckets:
                if b["status"] != "closed":
                    continue
                rd = readings.get((b["id"], "sell"))
                if rd is None or not rd.get("checklist"):
                    unlisted += 1
                elif b["cost"]:
                    sells.append({"reasons": rd.get("reasons") or [], "pnl_pct": b["realized_pnl"] / b["cost"] * 100.0})
                if b.get("parent_id"):
                    continue  # a part sold earlier: counted with its bucket
                parts = [b, *parts_of.get(b["id"], [])]
                cost = sum(p["cost"] for p in parts)
                rb = readings.get((b["id"], "buy"))
                if rb is None or not rb.get("checklist"):
                    unlisted += 1
                elif cost:
                    trades.append({"reasons": rb.get("reasons") or [], "pnl_pct": sum(p["realized_pnl"] for p in parts) / cost * 100.0})
        out = reason_stats_over(trades, sells)
        out.update(symbol=symbol, symbols=sorted(symbols), unlisted=unlisted)
        return out

    @staticmethod
    def _public_snapshot(row: dict) -> dict:
        return {
            "id": row["id"],
            "bucket_id": row["bucket_id"],
            "side": row["side"],
            "time_ms": row["time_ms"],
            "time_iso": _iso(row["time_ms"]),
            "interval": row["interval"],
            "has_image": bool(row["image_path"]),
            "readings": json.loads(row["readings"]),
        }

    # ----------------------------------------------------------------- helpers
    def _get(self, session_id: str) -> dict:
        with self.sessions.tx() as conn:
            s = self.sessions.get_session(conn, session_id)
        if s is None:
            raise NotFound(f"session {session_id} not found")
        return s

    def _get_ready(self, session_id: str) -> dict:
        s = self._get(session_id)
        if s["status"] != "ready":
            raise Conflict(f"session is {s['status']}" + (f": {s['error']}" if s.get("error") else ""))
        return s

    @staticmethod
    def _swing_only(s: dict) -> None:
        """Buckets, their trades and the plain step belong to swing sessions; a day-trading session
        is stepped and traded through the day-trading engine."""
        if (s.get("mode") or "swing") != "swing":
            raise Invalid("this is a day-trading session; use the day-trading controls")

    def _view_interval_ms(self, s: dict, interval: str | None) -> int:
        iv = interval or s["base_interval"]
        try:
            iv_ms = config.interval_ms(iv)
        except ValueError as exc:
            raise Invalid(str(exc)) from None
        if iv_ms < config.interval_ms(s["base_interval"]):
            raise Invalid(f"interval {iv} is finer than the session's base interval {s['base_interval']}")
        return iv_ms

    def _candles_at_cursor(self, s: dict, interval: str, limit: int | None, anchor_ms: int | None = None) -> pd.DataFrame:
        """The last `limit` candles of `interval` known at the cursor, the forming one included.

        Candles are read from `anchor_ms` on (default: the session's warm-up start); `limit` None
        returns them all."""
        base_ms = config.interval_ms(s["base_interval"])
        iv_ms = self._view_interval_ms(s, interval)
        cursor = s["cursor_ms"]
        cur_close = cursor + base_ms
        anchor = s["start_ms"] - WARMUP_BARS * iv_ms if anchor_ms is None else anchor_ms
        df = self.candles.get_candles(s["symbol"], interval, anchor, cur_close - iv_ms, limit=limit)
        partial_open = floor_to_slot(cursor, iv_ms)
        if partial_open + iv_ms > cur_close:
            base = self.candles.get_candles(s["symbol"], s["base_interval"], partial_open, cursor)
            if len(base):
                row = pd.DataFrame([aggregate(base, partial_open // 1000)])
                df = pd.concat([df, row], ignore_index=True) if len(df) else row
        return df

    def _rsi_at_cursor(self, s: dict, interval: str) -> float | None:
        """RSI(14) of `interval` on the latest candle visible at the cursor, as the chart shows it."""
        return self._rsi_from_df(self._candles_at_cursor(s, interval, RSI_WARMUP_BARS))

    @staticmethod
    def _rsi_from_df(df: pd.DataFrame) -> float | None:
        if len(df) <= RSI_PERIOD:
            return None
        value = float(core.rsi(df["close"], RSI_PERIOD).iloc[-1])
        return value if math.isfinite(value) else None

    @staticmethod
    def _bollinger_from_df(df: pd.DataFrame, price: float) -> dict:
        """Bollinger 20/2 on the latest candle: bands, %B (0 = lower band, 1 = upper) and width."""
        empty = {"upper": None, "middle": None, "lower": None, "pctb": None, "width_pct": None}
        if len(df) < BB_PERIOD:
            return empty
        last = core.bollinger(df["close"], BB_PERIOD, BB_MULT).iloc[-1]
        upper, middle, lower = float(last["upper"]), float(last["middle"]), float(last["lower"])
        if not all(math.isfinite(v) for v in (upper, middle, lower)) or middle <= 0:
            return empty
        span = upper - lower
        return {
            "upper": upper, "middle": middle, "lower": lower,
            "pctb": (price - lower) / span if span > 0 else None,
            "width_pct": span / middle * 100.0,
        }

    def _price(self, s: dict) -> float:
        df = self.candles.get_candles(s["symbol"], s["base_interval"], s["cursor_ms"], s["cursor_ms"])
        if len(df) != 1:
            raise Conflict(f"no base candle at cursor {_iso(s['cursor_ms'])}; data may have been deleted")
        return float(df["close"].iloc[0])

    @staticmethod
    def _public_session(s: dict) -> dict:
        out = dict(s)
        out["mode"] = s.get("mode") or "swing"
        for k in ("start_ms", "end_ms", "data_start_ms", "data_end_ms", "cursor_ms"):
            out[k.replace("_ms", "_iso")] = _iso(s[k])
        return out

    @staticmethod
    def _public_bucket(b: dict, price: float | None, excursion: dict | None = None, siblings: list[dict] | None = None) -> dict:
        """Bucket row for the API. `excursion` is the running high/low of an open bucket (see
        `_excursion`); a closed bucket carries its own from the sell. `siblings` (all buckets of
        the session) gives an open bucket its `parts_sold` count."""
        out = dict(b)
        out["parts_sold"] = sum(1 for x in siblings if x.get("parent_id") == b["id"]) if siblings and b["status"] == "open" else 0
        if b["status"] == "closed" and b.get("held_high") is not None:
            excursion = {"high": b["held_high"], "high_ms": b["held_high_ms"], "low": b["held_low"], "low_ms": b["held_low_ms"]}
        elif b["status"] == "closed":
            excursion = None
        for k in ("high", "low"):
            v = excursion[k] if excursion else None
            ms = excursion[f"{k}_ms"] if excursion else None
            out[f"held_{k}"] = v
            out[f"held_{k}_ms"] = ms
            out[f"held_{k}_iso"] = _iso(ms) if ms is not None else None
            out[f"held_{k}_pct"] = (v / b["entry_price"] - 1.0) * 100.0 if v is not None and b["entry_price"] else None
        out["opened_iso"] = _iso(b["opened_ms"])
        out["closed_iso"] = _iso(b["closed_ms"])
        for k in ("tp_pct", "sl_pct", "tp_price", "sl_price", "armed_ms", "trigger"):
            out.setdefault(k, None)
        out["armed_iso"] = _iso(b["armed_ms"]) if b.get("armed_ms") is not None else None
        out["tp_dist_pct"] = (b["tp_price"] / price - 1.0) * 100.0 if b["status"] == "open" and price and b.get("tp_price") else None
        out["sl_dist_pct"] = (b["sl_price"] / price - 1.0) * 100.0 if b["status"] == "open" and price and b.get("sl_price") else None
        if b["status"] == "open" and price is not None:
            mv = b["qty"] * price
            out["market_value"] = mv
            out["unrealized_pnl"] = mv - b["cost"]
            out["unrealized_pnl_pct"] = (mv / b["cost"] - 1.0) * 100.0
        else:
            out["market_value"] = None
            out["unrealized_pnl"] = None
            out["unrealized_pnl_pct"] = None
        if b["status"] == "closed":
            out["realized_pnl_pct"] = (b["realized_pnl"] / b["cost"]) * 100.0
            side = lambda p: {k: b.get(f"{p}_{k}") for k in ("ema200", "sma200", "ema200_pct", "sma200_pct")}  # noqa: E731
            out.update(ReplayEngine.long_avg_changes(side("buy"), side("sell")))
        else:
            out["realized_pnl_pct"] = None
            out.update({k: None for k in ("ema200_change_pct", "ema200_dist_change", "sma200_change_pct", "sma200_dist_change")})
        return out
